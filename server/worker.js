import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import fs from "fs";
import os from "os";
import path from "path";
import axios from "axios";
import FormData from "form-data";
import { Worker, Queue } from "bullmq";
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

// 用于排定"闲置超时"延时任务（warn/cancel）。与网关同一个队列，本 worker 自己消费。
const timers = new Queue(queueName, { connection: redis });

const worker = new Worker(queueName, async (job) => {
  console.log(`[worker] starting ${job.name} ${job.id}`);
  try {
    switch (job.name) {
      case "edit-video": return editVideo(job.data);
      case "confirm-job": return confirmJob(job.data);
      case "render-job": return renderJob(job.data);
      case "cancel-job": return cancelJob(job.data);
      case "revise-job": return reviseJob(job.data);
      case "send-help": return sendHelp(job.data.waNumber);
      case "collect-ack": return collectAck(job.data);
      case "collect-nudge": return collectNudge(job.data);
      case "collect-cancel": return collectCancel(job.data);
      case "collect-note": return collectNote(job.data);
      case "finalize-collection": return finalizeCollection(job.data);
      case "collection-choice": return collectionChoice(job.data);
      case "idle-warn": return idleWarn(job.data);
      case "idle-cancel": return idleCancel(job.data);
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

// 内部/后台任务失败不向用户报错（避免超时定时器等误发"job failed"）
const _SILENT_FAIL_JOBS = new Set(["idle-warn", "idle-cancel", "collect-ack", "collect-nudge", "collect-note"]);

worker.on("failed", async (job, error) => {
  console.error(`[worker] failed ${job?.name} ${job?.id}: ${error.message}`);
  const waNumber = job?.data?.waNumber;
  if (waNumber && !_SILENT_FAIL_JOBS.has(job?.name)) {
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
      Number(env("WA_PLAN_TIMEOUT_MS", "180000")));

    if (status.status === "ERROR") {
      throw new Error(status.error_message || "Python planning failed");
    }
    if (status.status === "NEEDS_CLARIFICATION") {
      const q = status.planned_edit?.clarification_question || "我需要更多信息才能编辑这段视频。";
      await sendText(waNumber, `${q}\n\n请补充具体细节（例如从第几秒到第几秒），然后重新发送这段视频。`);
      return;
    }
    if (status.status === "PREVIEW_READY") {
      await sendText(waNumber, previewText(jobId));
      await armIdle(waNumber, jobId, "export");
      return;
    }
    await sendText(waNumber, formatPlanMessage(status) + planIdleNotice());
    await armIdle(waNumber, jobId, "confirm");
  } finally {
    await fs.promises.rm(tempPath, { force: true });
  }
}

async function confirmJob({ waNumber, jobId }) {
  await disarmIdle(waNumber); // 用户已确认，作废"等待确认"的超时
  await postPython(`/jobs/${encodeURIComponent(jobId)}/confirm`);
  await sendText(waNumber, "Confirmed. Editing video now...");
  const status = await waitForStatus(jobId,
    ["PREVIEW_READY", "ERROR"],
    Number(env("WA_PIPELINE_TIMEOUT_MS", "900000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Python pipeline failed");
  }
  await sendText(waNumber, previewText(jobId));
  await armIdle(waNumber, jobId, "export"); // 进入"等待导出"，重新计时
}

async function renderJob({ waNumber, jobId }) {
  await disarmIdle(waNumber); // 用户已导出，作废"等待导出"的超时
  await postPython(`/jobs/${encodeURIComponent(jobId)}/render`);
  await sendText(waNumber, "Export started. Will send the final video when ready.");
  const status = await waitForStatus(jobId,
    ["DONE", "ERROR"],
    Number(env("WA_RENDER_TIMEOUT_MS", "900000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Python render failed");
  }

  // 成品通常 > 16MB，超出 WhatsApp 视频消息上限，统一以链接投递（走 PUBLIC_BASE_URL）
  await sendText(waNumber, `Your final video is ready: ${fileUrl(jobId, "final.mp4")}`);
  await redis.del(activeJobKey(waNumber));
}

async function cancelJob({ waNumber, jobId }) {
  await disarmIdle(waNumber); // 作废挂起的超时任务
  await redis.del(activeJobKey(waNumber));
  await sendText(waNumber, `Cancelled job ${jobId}. Send a new video when ready.`);
}

async function sendHelp(waNumber) {
  await sendText(waNumber,
    "发一段视频并配上说明（例如：去掉空白、加中文字幕）。\n" +
    "想加 b-roll？先发主视频、再发每段补充画面（各自配一句“讲到X时放这段”），" +
    "全部发完回复 *go* 开始。");
}

// ── b-roll 收集态 ──────────────────────────────────────────────
// 每收到一条素材回一句引导；用户回 'go' 收尾。2+ 视频时问哪个是主视频。

async function collectAck({ waNumber, count, kind, caption }) {
  const noun = kind === "image" ? "图片" : "视频";
  const note = caption ? `（说明：${caption}）` : "";
  await safeSendText(waNumber,
    `已收到第 ${count} 个${noun}${note}。\n` +
    `可以继续发素材，也可以直接用文字描述（例如“视频1是主视频加字幕；视频2讲到 VS Code 时插入”）。` +
    `全部发完后回复 *go*（或“开始/完成”）即可开始，回复 *cancel* 取消。`);
}

// 收集期发来的独立文字：存进 notes 缓冲，供 go 时的 LLM 解析用
async function collectNote({ waNumber }) {
  await safeSendText(waNumber,
    `已记下你的描述。可继续发素材或补充描述；发完回复 *go* 开始。`);
}

async function collectNudge({ waNumber, count }) {
  await safeSendText(waNumber,
    `已收到 ${count} 个素材。发完后回复 *go* 开始剪辑，回复 *cancel* 清空重来。`);
}

async function collectCancel({ waNumber }) {
  await redis.del(notesKey(waNumber));
  await safeSendText(waNumber, "已清空本次素材与描述。重新发送视频即可开始。");
}

async function finalizeCollection({ waNumber }) {
  const items = (await redis.lrange(collectKey(waNumber), 0, -1)).map((s) => JSON.parse(s));
  const videos = items.filter((i) => i.kind === "video");
  if (videos.length === 0) {
    await safeSendText(waNumber, "还没有收到主视频。请先发送一段你要编辑的视频，再回复 *go*。");
    return;
  }
  // 汇总描述：各媒体配文 + 收集期发来的独立文字；交给 Python 的 LLM 解析角色/说明/编辑要求
  const notes = await buildNotes(waNumber, items);
  const assign = await postAssign(videos.length, notes);

  if (videos.length === 1) {
    // 只有一个视频，它就是主，无需询问
    await startWithMain(waNumber, items, videos[0], assign);
    return;
  }
  const mainNum = Number(assign && assign.main_index);
  if (mainNum >= 1 && mainNum <= videos.length) {
    // LLM 从文字判断出了主视频，直接开跑
    await startWithMain(waNumber, items, videos[mainNum - 1], assign);
    return;
  }
  // 判不出主视频 → 保留交互，问编号；把 assign 存起来，编号回来后复用其 labels/edit_request
  await redis.set(assignKey(waNumber), JSON.stringify(assign || {}), "EX", Number(env("WA_COLLECT_TTL", "3600")));
  await redis.set(awaitChoiceKey(waNumber), "1", "EX", Number(env("WA_COLLECT_TTL", "3600")));
  const lines = ["没看出哪个是*主视频*（出镜/口播那条）。可以：回复编号选一个，或再用一句话补充说明（例如“视频1是主视频，视频2讲到 VS Code 时插入”），我据此安排；其余作为 b-roll："];
  videos.forEach((v, i) => lines.push(`${i + 1}. 视频${v.caption ? ` — ${v.caption}` : ""}`));
  await safeSendText(waNumber, lines.join("\n"));
}

async function collectionChoice({ waNumber, choice }) {
  const items = (await redis.lrange(collectKey(waNumber), 0, -1)).map((s) => JSON.parse(s));
  const videos = items.filter((i) => i.kind === "video");
  if (videos.length === 0) {
    // 素材已过期/被清空 → 退出选择态，别把用户卡住
    await redis.del(awaitChoiceKey(waNumber));
    await redis.del(assignKey(waNumber));
    await safeSendText(waNumber, "素材好像已经过期或清空了，请重新发送视频后再回复 *go*。");
    return;
  }
  const text = String(choice || "").trim();
  const isBareNumber = /^\s*\d+\s*$/.test(text);

  // 情况一：只回了一个编号 → 直接选主视频（沿用之前缓存的 labels/edit_request）
  if (isBareNumber) {
    const n = parseInt(text, 10);
    if (n < 1 || n > videos.length) {
      await safeSendText(waNumber, `请回复 1-${videos.length} 之间的编号，选择主视频。`);
      return;
    }
    let assign = { main_index: n, labels: {}, edit_request: "" };
    const raw = await redis.get(assignKey(waNumber));
    if (raw) { try { assign = JSON.parse(raw); } catch (e) { /* 用默认 */ } }
    assign.main_index = n;  // 用户手选的编号优先于 LLM 的判断
    await redis.del(awaitChoiceKey(waNumber));
    await redis.del(assignKey(waNumber));
    await startWithMain(waNumber, items, videos[n - 1], assign);
    return;
  }

  // 情况二：发的是一段文字说明 → 存进 notes，重新用 LLM 解析角色/说明/编辑要求
  // （修复：以前这里只认编号，用户在“问主视频”阶段发的描述会被拒绝、丢失）
  await redis.rpush(notesKey(waNumber), text);
  await redis.expire(notesKey(waNumber), Number(env("WA_COLLECT_TTL", "3600")));
  const notes = await buildNotes(waNumber, items);
  const assign = await postAssign(videos.length, notes);
  const mainNum = Number(assign && assign.main_index);
  if (mainNum >= 1 && mainNum <= videos.length) {
    // 这次文字里能判出主视频 → 直接开跑
    await redis.del(awaitChoiceKey(waNumber));
    await redis.del(assignKey(waNumber));
    await startWithMain(waNumber, items, videos[mainNum - 1], assign);
    return;
  }
  // 仍判不出主视频 → 缓存这次解析出的 labels/edit_request，继续问编号
  await redis.set(assignKey(waNumber), JSON.stringify(assign || {}), "EX", Number(env("WA_COLLECT_TTL", "3600")));
  const lines = ["收到你的说明。还差一步：哪个是*主视频*（出镜/口播那条）？回一个编号即可，其余作为 b-roll："];
  videos.forEach((v, i) => lines.push(`${i + 1}. 视频${v.caption ? ` — ${v.caption}` : ""}`));
  await safeSendText(waNumber, lines.join("\n"));
}

// 用 assign 的 label/edit_request 组装 b-roll 列表并开跑
async function startWithMain(waNumber, items, mainItem, assign) {
  const videos = items.filter((i) => i.kind === "video");
  const labels = (assign && assign.labels) || {};
  const brollItems = items.filter((i) => i !== mainItem).map((i) => {
    let label = i.caption || "";
    if (i.kind === "video") {
      const num = videos.indexOf(i) + 1;  // 该视频的上传编号（1 开始）
      label = labels[String(num)] || labels[num] || i.caption || "";
    }
    return { ...i, label };
  });
  const editRequest = (assign && assign.edit_request) || mainItem.caption || "";
  await runCollectionJob(waNumber, mainItem, brollItems, editRequest);
}

// 汇总描述文字：各媒体 caption + 收集期独立文字（notes 缓冲）
async function buildNotes(waNumber, items) {
  const parts = [];
  const videos = items.filter((i) => i.kind === "video");
  items.forEach((i) => {
    if (i.caption) {
      const tag = i.kind === "video" ? `视频${videos.indexOf(i) + 1}` : "图片";
      parts.push(`[${tag} 配文] ${i.caption}`);
    }
  });
  const notes = await redis.lrange(notesKey(waNumber), 0, -1);
  notes.forEach((t) => parts.push(t));
  return parts.join("\n");
}

// 让 Python 用 LLM 解析：{main_index(1开始|null), labels{视频号:说明}, edit_request}
async function postAssign(videoCount, notes) {
  const fallback = { main_index: null, labels: {}, edit_request: notes || "" };
  try {
    const params = new URLSearchParams();
    params.append("video_count", String(videoCount));
    params.append("notes", notes || "");
    const resp = await axios.post(`${pythonApiBase}/assign`, params.toString(), {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      timeout: Number(env("WA_PYTHON_POST_TIMEOUT_MS", "30000")),
    });
    return resp.data || fallback;
  } catch (err) {
    console.warn(`[worker] /assign failed, fallback to ask-by-number: ${err.message}`);
    return fallback;
  }
}

// 下载主视频 + 所有 b-roll → 一次性 POST /jobs → 设活跃任务 → 等方案 → 回方案
async function runCollectionJob(waNumber, mainItem, brollItems, editRequest) {
  if (!hasWACredentials()) {
    await sendText(waNumber, "Service is starting up. Please try again in a moment.");
    throw new Error("WhatsApp credentials not configured");
  }
  // 原子认领：DEL 返回被删键数。两条 'go'/两次编号并发时只有一个删到（返回 1），
  // 其余返回 0 直接退出，避免重复建任务（对抗性审查 #4）。
  const claimed = await redis.del(collectKey(waNumber));
  if (!claimed) return;
  await redis.del(notesKey(waNumber));  // 清描述缓冲
  const brollNote = brollItems.length ? `，并叠加 ${brollItems.length} 段 b-roll` : "";
  await sendText(waNumber, `开始处理主视频${brollNote}，正在下载素材并生成剪辑方案...`);

  const tempPaths = [];
  try {
    const mainPath = await downloadWhatsAppMedia(mainItem.mediaId, "video");
    tempPaths.push(mainPath);
    const brollPaths = [];
    for (const b of brollItems) {
      const p = await downloadWhatsAppMedia(b.mediaId, b.kind);
      tempPaths.push(p);
      brollPaths.push({ path: p, label: b.label || b.caption || "", kind: b.kind });
    }
    const created = await createPythonJobMulti(
      mainPath,
      editRequest || mainItem.caption || "Remove blank parts, add subtitles, and make it flow smoothly.",
      brollPaths);
    const jobId = created.job_id;
    await redis.set(activeJobKey(waNumber), jobId, "EX", Number(env("WA_ACTIVE_JOB_TTL", "86400")));

    const status = await waitForStatus(jobId,
      ["WAITING_CONFIRMATION", "NEEDS_CLARIFICATION", "PREVIEW_READY", "ERROR"],
      Number(env("WA_PLAN_TIMEOUT_MS", "180000")));

    if (status.status === "ERROR") {
      throw new Error(status.error_message || "Python planning failed");
    }
    if (status.status === "NEEDS_CLARIFICATION") {
      const q = status.planned_edit?.clarification_question || "我需要更多信息才能编辑这段视频。";
      await sendText(waNumber, `${q}\n\n请补充细节后重新发送素材。`);
      return;
    }
    if (status.status === "PREVIEW_READY") {
      await sendText(waNumber, previewText(jobId));
      await armIdle(waNumber, jobId, "export");
      return;
    }
    await sendText(waNumber, formatPlanMessage(status) + planIdleNotice());
    await armIdle(waNumber, jobId, "confirm");
  } finally {
    for (const p of tempPaths) await fs.promises.rm(p, { force: true });
  }
}

async function createPythonJobMulti(videoPath, editRequest, brollPaths) {
  const form = new FormData();
  form.append("video", fs.createReadStream(videoPath),
    { filename: "input.mp4", contentType: "video/mp4" });
  form.append("edit_request", editRequest);
  form.append("pipeline", "talking-head");
  brollPaths.forEach((b, i) => {
    const ext = path.extname(b.path) || (b.kind === "image" ? ".jpg" : ".mp4");
    const ctype = b.kind === "image" ? "image/jpeg" : "video/mp4";
    form.append("broll", fs.createReadStream(b.path),
      { filename: `broll_${i}${ext}`, contentType: ctype });
    form.append("broll_labels", b.label || "");
    form.append("broll_kinds", b.kind || "video");
  });
  const resp = await axios.post(`${pythonApiBase}/jobs`, form, {
    headers: form.getHeaders(),
    maxBodyLength: Infinity, maxContentLength: Infinity,
    timeout: Number(env("WA_PYTHON_CREATE_TIMEOUT_MS", "180000")),
  });
  return resp.data;
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

async function postPythonForm(pathname, text) {
  const params = new URLSearchParams();
  params.append("text", text || "");
  const resp = await axios.post(`${pythonApiBase}${pathname}`, params.toString(), {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    timeout: Number(env("WA_PYTHON_POST_TIMEOUT_MS", "30000")),
  });
  return resp.data;
}

// 就地修订：用户在方案/预览阶段直接打字提意见 → Python 带反馈重规划 → 回新方案
async function reviseJob({ waNumber, jobId, text }) {
  await disarmIdle(waNumber); // 用户发来修改意见＝有操作，作废旧超时；重规划后再计时
  await sendText(waNumber, "收到修改意见，正在重新规划...");
  await postPythonForm(`/jobs/${encodeURIComponent(jobId)}/revise`, text);
  const status = await waitForStatus(jobId,
    ["WAITING_CONFIRMATION", "NEEDS_CLARIFICATION", "ERROR"],
    Number(env("WA_PLAN_TIMEOUT_MS", "180000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Revise failed");
  }
  if (status.status === "NEEDS_CLARIFICATION") {
    const q = status.planned_edit?.clarification_question || "需要更多信息才能继续。";
    await sendText(waNumber, q);
    return;
  }
  await sendText(waNumber, formatPlanMessage(status) + planIdleNotice());
  await armIdle(waNumber, jobId, "confirm"); // 新方案又回到"等待确认"，重新计时
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

async function downloadWhatsAppMedia(mediaId, kind = "video") {
  const token = whatsappToken();
  const info = await axios.get(`${graphBase}/${mediaId}`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: Number(env("WA_MEDIA_INFO_TIMEOUT_MS", "30000")),
  });
  const mediaUrl = info.data.url;
  if (!mediaUrl) throw new Error(`WhatsApp media ${mediaId} no download URL`);
  const mime = info.data.mime_type || "";
  const ext = kind === "image"
    ? (mime.includes("png") ? "png" : mime.includes("webp") ? "webp" : "jpg")
    : "mp4";
  const outPath = path.join(os.tmpdir(), `openmontage-wa-${mediaId}.${ext}`);
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

// ── 闲置超时（等待用户 confirm/export 时自动取消）────────────────────────────
// await 键存当前等待态；gen 是"代次"计数：每推进/取消一步就 INCR 一次，
// 之前排定的 warn/cancel 延时任务醒来时发现代次已变，就自作废（= 重置计时）。
function awaitKey(waNumber) { return `wa:user:${waNumber}:await`; }
function awaitGenKey(waNumber) { return `wa:user:${waNumber}:await_gen`; }
function idleTimeoutMs() { return Number(env("WA_IDLE_TIMEOUT_MS", "1800000")); } // 默认 30 分钟
function idleWarnMs() { return Number(env("WA_IDLE_WARN_MS", "300000")); }        // 到期前 5 分钟提醒
function idleMins() { return Math.max(1, Math.round(idleTimeoutMs() / 60000)); }
function idleWarnMins() { return Math.max(1, Math.round(idleWarnMs() / 60000)); }

// 排定本次等待的超时提醒 + 自动取消。stage: "confirm" | "export"
async function armIdle(waNumber, jobId, stage) {
  const gen = await redis.incr(awaitGenKey(waNumber));
  const ttl = Math.ceil(idleTimeoutMs() / 1000) + 120;
  await redis.set(awaitKey(waNumber), JSON.stringify({ jobId, stage, gen }), "EX", ttl);
  await redis.expire(awaitGenKey(waNumber), ttl);
  const data = { waNumber, jobId, stage, gen };
  const base = { removeOnComplete: true, removeOnFail: true };
  await timers.add("idle-warn", data,
    { ...base, jobId: `iw:${waNumber}:${gen}`, delay: Math.max(1000, idleTimeoutMs() - idleWarnMs()) });
  await timers.add("idle-cancel", data,
    { ...base, jobId: `ic:${waNumber}:${gen}`, delay: idleTimeoutMs() });
}

// 用户推进/取消一步 → 作废当前等待的超时任务（bump 代次），清等待态
async function disarmIdle(waNumber) {
  await redis.incr(awaitGenKey(waNumber));
  await redis.del(awaitKey(waNumber));
}

// 延时任务醒来时：仅当代次未变且活跃任务仍是它，才算有效
async function _idleStillValid(waNumber, jobId, gen) {
  const cur = Number(await redis.get(awaitGenKey(waNumber)));
  if (cur !== Number(gen)) return false;
  const active = await redis.get(activeJobKey(waNumber));
  return active === jobId;
}

async function idleWarn({ waNumber, jobId, stage, gen }) {
  if (!(await _idleStillValid(waNumber, jobId, gen))) return;
  const act = stage === "export" ? "export（导出）或 cancel（取消）" : "confirm（确认）或 cancel（取消）";
  await safeSendText(waNumber,
    `提醒：本次剪辑约 ${idleWarnMins()} 分钟后将因无操作自动取消。回复 ${act} 即可继续。`);
}

async function idleCancel({ waNumber, jobId, stage, gen }) {
  if (!(await _idleStillValid(waNumber, jobId, gen))) return;
  await redis.del(activeJobKey(waNumber));
  await redis.del(awaitKey(waNumber));
  await redis.incr(awaitGenKey(waNumber)); // 再 bump，防止残留延时任务重复触发
  await safeSendText(waNumber,
    "本次剪辑因长时间无操作已自动取消。需要的话重新发送视频即可重新开始。");
}

// 预览消息文案：附上"可 cancel 取消"与"多久后自动取消"的提示
function previewText(jobId) {
  return `Preview ready: ${fileUrl(jobId, "preview.mp4")}\n` +
    "Reply export to generate final video, or cancel to discard.\n" +
    `（请在 ${idleMins()} 分钟内回复，否则将自动取消本次剪辑）`;
}

// 方案消息后缀：告诉用户多久内不确认会自动取消
function planIdleNotice() {
  return `\n（请在 ${idleMins()} 分钟内回复 confirm/cancel，否则将自动取消本次剪辑）`;
}

function collectKey(waNumber) {
  return `wa:user:${waNumber}:collect`;
}

function awaitChoiceKey(waNumber) {
  return `wa:user:${waNumber}:await_choice`;
}

// 收集期用户发来的独立描述文字（LIST）
function notesKey(waNumber) {
  return `wa:user:${waNumber}:notes`;
}

// 判不出主视频时，暂存 LLM 的 labels/edit_request，等用户回编号后复用
function assignKey(waNumber) {
  return `wa:user:${waNumber}:assign`;
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