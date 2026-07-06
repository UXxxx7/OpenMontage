import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import fs from "fs";
import os from "os";
import path from "path";
import axios from "axios";
import FormData from "form-data";
import { Worker } from "bullmq";
import IORedis from "ioredis";

config({ path: resolve(dirname(fileURLToPath(import.meta.url)), "../.env") });

const REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0");
const queueName = env("WA_QUEUE_NAME", "openmontage-video-jobs");
const graphVersion = env("WA_GRAPH_VERSION", "v21.0");
const graphBase = `https://graph.facebook.com/${graphVersion}`;
const pythonApiBase = env("OPENMONTAGE_API_BASE", "http://localhost:8000").replace(/\/$/, "");

const redis = new IORedis(REDIS_URL, {
  maxRetriesPerRequest: null,
  connectTimeout: Number(env("WA_REDIS_CONNECT_TIMEOUT_MS", "5000")),
  retryStrategy(times) {
    if (times > 10) return null;
    return Math.min(times * 200, 3000);
  },
});

async function checkReadiness() {
  const results = {};
  try {
    await redis.ping();
    results.redis = "ok";
  } catch (err) {
    results.redis = err.message;
  }
  try {
    await axios.get(`${pythonApiBase}/health`, {
      timeout: Number(env("WA_STARTUP_CHECK_TIMEOUT_MS", "10000")),
    });
    results.python = "ok";
  } catch (err) {
    results.python = err.message;
  }
  return results;
}

const worker = new Worker(queueName, async (job) => {
  console.log(`[worker] starting ${job.name} ${job.id}`);
  try {
    switch (job.name) {
      case "edit-video": return editVideo(job.data);
      case "confirm-job": return confirmJob(job.data);
      case "render-job": return renderJob(job.data);
      case "cancel-job": return cancelJob(job.data);
      case "send-help": return sendHelp(job.data.waNumber);
      default: throw new Error(`Unknown job type: ${job.name}`);
    }
  } catch (err) {
    console.error(`[worker] ${job.name} ${job.id} error:`, err.message);
    throw err;
  }
}, {
  connection: redis,
  concurrency: Number(env("WA_WORKER_CONCURRENCY", "2")),
  limiter: {
    max: Number(env("WA_WORKER_RATE_MAX", "4")),
    duration: Number(env("WA_WORKER_RATE_WINDOW_MS", "60000")),
  },
});

worker.on("completed", (job) => {
  console.log(`[worker] completed ${job.name} ${job.id}`);
});

worker.on("failed", async (job, error) => {
  console.error(`[worker] failed ${job?.name} ${job?.id}: ${error.message}`);
  const waNumber = job?.data?.waNumber;
  if (waNumber) {
    await safeSendText(waNumber, "Sorry, the video job failed. Please try again.");
  }
});

async function editVideo({ waNumber, mediaId, editRequest }) {
  if (!hasWACredentials()) {
    await sendText(waNumber, "Service is starting up. Please send your video again in a moment.");
    throw new Error("WhatsApp credentials not configured");
  }
  await sendText(waNumber, "Video received. Downloading and preparing edit plan...");

  const tempPath = await downloadWhatsAppMedia(mediaId);
  try {
    const created = await createPythonJob(tempPath,
      editRequest || "Remove blank parts, add subtitles, and make it flow smoothly.");
    const jobId = created.job_id;
    await redis.set(activeJobKey(waNumber), jobId, "EX",
      Number(env("WA_ACTIVE_JOB_TTL", "86400")));

    const status = await waitForStatus(jobId,
      ["WAITING_CONFIRMATION", "NEEDS_CLARIFICATION", "PREVIEW_READY", "ERROR"],
      Number(env("WA_PLAN_TIMEOUT_MS", "120000")));

    if (status.status === "ERROR") {
      throw new Error(status.error_message || "Python planning failed");
    }
    if (status.status === "NEEDS_CLARIFICATION") {
      const q = status.planned_edit?.clarification_question || "我需要更多信息才能编辑这段视频。";
      await sendText(waNumber, `${q}\n\n请补充具体细节（例如从第几秒到第几秒），然后重新发送这段视频。`);
      return;
    }
    if (status.status === "PREVIEW_READY") {
      await sendText(waNumber, `Preview ready: ${fileUrl(jobId, "preview.mp4")}\nReply export to generate final video.`);
      return;
    }
    await sendText(waNumber, formatPlanMessage(status));
  } finally {
    await fs.promises.rm(tempPath, { force: true });
  }
}

async function confirmJob({ waNumber, jobId }) {
  await postPython(`/jobs/${encodeURIComponent(jobId)}/confirm`);
  await sendText(waNumber, "Confirmed. Editing video now...");
  const status = await waitForStatus(jobId,
    ["PREVIEW_READY", "ERROR"],
    Number(env("WA_PIPELINE_TIMEOUT_MS", "900000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Python pipeline failed");
  }
  await sendText(waNumber, `Preview ready: ${fileUrl(jobId, "preview.mp4")}\nReply export to generate final video.`);
}

async function renderJob({ waNumber, jobId }) {
  await postPython(`/jobs/${encodeURIComponent(jobId)}/render`);
  await sendText(waNumber, "Export started. Will send the final video when ready.");
  const status = await waitForStatus(jobId,
    ["DONE", "ERROR"],
    Number(env("WA_RENDER_TIMEOUT_MS", "900000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Python render failed");
  }

  const finalPath = status.final_path;
  if (finalPath && fs.existsSync(finalPath)) {
    await sendVideo(waNumber, finalPath, "Your final video is ready.");
  } else {
    await sendText(waNumber, `Final video: ${fileUrl(jobId, "final.mp4")}`);
  }
  await redis.del(activeJobKey(waNumber));
}

async function cancelJob({ waNumber, jobId }) {
  await redis.del(activeJobKey(waNumber));
  await sendText(waNumber, `Cancelled job ${jobId}. Send a new video when ready.`);
}

async function sendHelp(waNumber) {
  await sendText(waNumber,
    "Send a video with a caption like: Remove blank parts, add Chinese subtitles.\nThen reply confirm, export, or cancel when prompted.");
}

async function createPythonJob(videoPath, editRequest) {
  const form = new FormData();
  form.append("video", fs.createReadStream(videoPath),
    { filename: "input.mp4", contentType: "video/mp4" });
  form.append("edit_request", editRequest);
  form.append("pipeline", "talking-head");
  const resp = await axios.post(`${pythonApiBase}/jobs`, form, {
    headers: form.getHeaders(),
    maxBodyLength: Infinity, maxContentLength: Infinity,
    timeout: Number(env("WA_PYTHON_CREATE_TIMEOUT_MS", "180000")),
  });
  return resp.data;
}

async function getPythonJob(jobId) {
  const resp = await axios.get(`${pythonApiBase}/jobs/${encodeURIComponent(jobId)}`, {
    timeout: Number(env("WA_PYTHON_GET_TIMEOUT_MS", "30000")),
  });
  return resp.data;
}

async function postPython(pathname) {
  const resp = await axios.post(`${pythonApiBase}${pathname}`, null, {
    timeout: Number(env("WA_PYTHON_POST_TIMEOUT_MS", "30000")),
  });
  return resp.data;
}

async function waitForStatus(jobId, wanted, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const last = await getPythonJob(jobId);
    if (wanted.includes(last.status)) return last;
    await delay(Number(env("WA_STATUS_POLL_MS", "3000")));
  }
  throw new Error(`Timed out waiting for ${jobId}`);
}

async function downloadWhatsAppMedia(mediaId) {
  const token = whatsappToken();
  const info = await axios.get(`${graphBase}/${mediaId}`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: Number(env("WA_MEDIA_INFO_TIMEOUT_MS", "30000")),
  });
  const mediaUrl = info.data.url;
  if (!mediaUrl) throw new Error(`WhatsApp media ${mediaId} no download URL`);
  const outPath = path.join(os.tmpdir(), `openmontage-wa-${mediaId}.mp4`);
  const resp = await axios.get(mediaUrl, {
    headers: { Authorization: `Bearer ${token}` },
    responseType: "stream",
    timeout: Number(env("WA_MEDIA_DOWNLOAD_TIMEOUT_MS", "180000")),
  });
  await new Promise((resolvePromise, rejectPromise) => {
    const stream = fs.createWriteStream(outPath);
    resp.data.pipe(stream);
    stream.on("finish", resolvePromise);
    stream.on("error", rejectPromise);
  });
  return outPath;
}

async function sendText(to, body) {
  await axios.post(`${graphBase}/${whatsappPhoneId()}/messages`, {
    messaging_product: "whatsapp", recipient_type: "individual", to,
    type: "text", text: { preview_url: true, body },
  }, { headers: authJsonHeaders(), timeout: Number(env("WA_SEND_TIMEOUT_MS", "30000")) });
}

async function safeSendText(to, body) {
  try {
    await sendText(to, body);
  } catch (err) {
    console.warn(`[worker] sendText failed to ${to}: ${err.message}`);
  }
}

async function sendVideo(to, videoPath, caption = "") {
  const form = new FormData();
  form.append("file", fs.createReadStream(videoPath), { contentType: "video/mp4" });
  form.append("type", "video/mp4");
  form.append("messaging_product", "whatsapp");
  const upload = await axios.post(`${graphBase}/${whatsappPhoneId()}/media`, form, {
    headers: { ...form.getHeaders(), Authorization: `Bearer ${whatsappToken()}` },
    maxBodyLength: Infinity, maxContentLength: Infinity,
    timeout: Number(env("WA_MEDIA_UPLOAD_TIMEOUT_MS", "180000")),
  });
  await axios.post(`${graphBase}/${whatsappPhoneId()}/messages`, {
    messaging_product: "whatsapp", recipient_type: "individual", to,
    type: "video", video: { id: upload.data.id, caption },
  }, { headers: authJsonHeaders(), timeout: Number(env("WA_SEND_TIMEOUT_MS", "30000")) });
}

function formatPlanMessage(job) {
  const plan = job.planned_edit || {};
  const lines = ["*Video edit plan*", "", plan.summary || "Edit plan prepared."];
  const ops = plan.edit_operations || [];
  if (ops.length) {
    lines.push("", "*Operations:*");
    ops.forEach((op, i) => lines.push(`${i + 1}. ${op.description || op.type || "Edit"}`));
  }
  lines.push("", "Reply *confirm* to start, or *cancel* to stop.");
  return lines.join("\n");
}

function fileUrl(jobId, filename) {
  const base = env("PUBLIC_BASE_URL", pythonApiBase).replace(/\/$/, "");
  return `${base}/files/${encodeURIComponent(jobId)}/${filename}`;
}

function activeJobKey(waNumber) {
  return `wa:user:${waNumber}:active_job`;
}

function authJsonHeaders() {
  return {
    Authorization: `Bearer ${whatsappToken()}`,
    "Content-Type": "application/json",
  };
}

function hasWACredentials() {
  return !!(whatsappToken() && whatsappPhoneId());
}

function whatsappToken() {
  return env("WA_TOKEN", env("WHATSAPP_ACCESS_TOKEN"));
}

function whatsappPhoneId() {
  return env("WA_PHONE_ID", env("WHATSAPP_PHONE_NUMBER_ID"));
}

function env(name, fallback = "") {
  return process.env[name] || fallback;
}

function delay(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function start() {
  const ready = await checkReadiness();
  console.log(`[worker] readiness: redis=${ready.redis} python=${ready.python}`);
  if (ready.redis !== "ok" || ready.python !== "ok") {
    console.warn("[worker] Some dependencies not ready. Worker will retry on first job.");
  }
  if (!hasWACredentials()) {
    console.warn("[worker] WhatsApp credentials not set. Media download/upload will fail until configured.");
  }
  console.log(`[worker] listening on queue ${queueName}; Python API ${pythonApiBase}`);
}

start();