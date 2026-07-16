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
import { resolveLang, t } from "./lang.js";

config({ path: resolve(dirname(fileURLToPath(import.meta.url)), "../.env") });

const REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0");
const queueName = env("WA_QUEUE_NAME", "openmontage-video-jobs");
const graphVersion = env("WA_GRAPH_VERSION", "v21.0");
const graphBase = `https://graph.facebook.com/${graphVersion}`;
const pythonApiBase = env("OPENMONTAGE_API_BASE", "http://localhost:8000").replace(/\/$/, "");
// 判不出语言时的兜底（比如失败通知这类完全没有触发文本可看的场景）。
const DEFAULT_LANG = env("WA_DEFAULT_LANG", "zh");

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
      case "croll-generate": return crollGenerate(job.data);
      case "confirm-job": return confirmJob(job.data);
      case "render-job": return renderJob(job.data);
      case "cancel-job": return cancelJob(job.data);
      case "retry-job": return retryJob(job.data);
      case "revise-job": return reviseJob(job.data);
      case "send-help": return sendHelp(job.data);
      case "answer-question": return answerQuestion(job.data);
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
    const lang = resolveLang(DEFAULT_LANG, job?.data?.text, job?.data?.editRequest);
    await safeSendText(waNumber, t(lang,
      "抱歉，视频处理任务失败了，请重新尝试。",
      "Sorry, the video job failed. Please try again."));
  }
});

async function editVideo({ waNumber, mediaId, editRequest }) {
  const lang = resolveLang(DEFAULT_LANG, editRequest);
  if (!hasWACredentials()) {
    await sendText(waNumber, t(lang,
      "服务正在启动，请稍后重新发送视频。",
      "Service is starting up. Please send your video again in a moment."));
    throw new Error("WhatsApp credentials not configured");
  }
  await sendText(waNumber, t(lang,
    "视频已收到，正在下载并生成剪辑方案...",
    "Video received. Downloading and preparing edit plan..."));

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
    const jobLang = resolveLang(lang, status.edit_request);

    if (status.status === "ERROR") {
      throw new Error(status.error_message || "Python planning failed");
    }
    if (status.status === "NEEDS_CLARIFICATION") {
      await sendText(waNumber, clarificationMessage(jobLang, status));
      return;
    }
    if (status.status === "PREVIEW_READY") {
      await sendText(waNumber, previewReadyMessage(jobLang, jobId, status.degraded_operations) + idleHint(jobLang, "export"));
      await armIdle(waNumber, jobId, "export", jobLang);
      return;
    }
    await sendText(waNumber, formatPlanMessage(status, jobLang) + idleHint(jobLang, "confirm"));
    await armIdle(waNumber, jobId, "confirm", jobLang);
  } finally {
    await fs.promises.rm(tempPath, { force: true });
  }
}

// C-roll：一张照片 -> Python 那边看图写文案 + HeyGen 生成数字人说话视频 ->
// 落地成 input.mp4 后自动接入常规规划管线。跟 editVideo 是同一个形状（下载
// 素材 -> 建 Python 任务 -> 等方案 -> 回复），区别只在素材是照片、Python 侧
// 多了一段生成耗时——所以用单独一个更长的超时（WA_CROLL_TIMEOUT_MS），
// 不跟普通视频规划的 WA_PLAN_TIMEOUT_MS 混用，免得两边互相牵制去调参数。
async function crollGenerate({ waNumber, mediaId, caption }) {
  const lang = resolveLang(DEFAULT_LANG, caption);
  if (!hasWACredentials()) {
    await sendText(waNumber, t(lang,
      "服务正在启动，请稍后重新发送照片。",
      "Service is starting up. Please send your photo again in a moment."));
    throw new Error("WhatsApp credentials not configured");
  }
  const tempPath = await downloadWhatsAppMedia(mediaId, "image");
  try {
    const created = await createPythonCrollJob(tempPath, lang, caption);
    const jobId = created.job_id;
    await redis.set(activeJobKey(waNumber), jobId, "EX",
      Number(env("WA_ACTIVE_JOB_TTL", "86400")));

    const status = await waitForStatus(jobId,
      ["WAITING_CONFIRMATION", "NEEDS_CLARIFICATION", "PREVIEW_READY", "ERROR"],
      Number(env("WA_CROLL_TIMEOUT_MS", "480000")));
    const jobLang = resolveLang(lang, status.edit_request);

    if (status.status === "ERROR") {
      throw new Error(status.error_message || "C-roll generation failed");
    }
    if (status.status === "NEEDS_CLARIFICATION") {
      await sendText(waNumber, clarificationMessage(jobLang, status));
      return;
    }
    if (status.status === "PREVIEW_READY") {
      await sendText(waNumber, previewReadyMessage(jobLang, jobId, status.degraded_operations, status.generation_cost_usd) + idleHint(jobLang, "export"));
      await armIdle(waNumber, jobId, "export", jobLang);
      return;
    }
    await sendText(waNumber, formatPlanMessage(status, jobLang) + idleHint(jobLang, "confirm"));
    await armIdle(waNumber, jobId, "confirm", jobLang);
  } finally {
    await fs.promises.rm(tempPath, { force: true });
  }
}

async function createPythonCrollJob(photoPath, lang, hint) {
  const form = new FormData();
  const ext = path.extname(photoPath) || ".jpg";
  form.append("photo", fs.createReadStream(photoPath),
    { filename: `photo${ext}`, contentType: ext === ".png" ? "image/png" : "image/jpeg" });
  form.append("hint", hint || "");
  form.append("lang", lang);
  form.append("pipeline", "talking-head");
  const resp = await axios.post(`${pythonApiBase}/croll`, form, {
    headers: form.getHeaders(),
    maxBodyLength: Infinity, maxContentLength: Infinity,
    timeout: Number(env("WA_PYTHON_CREATE_TIMEOUT_MS", "180000")),
  });
  return resp.data;
}

async function confirmJob({ waNumber, jobId }) {
  await disarmIdle(waNumber); // 用户已确认，作废"等待确认"的超时
  const before = await getPythonJob(jobId).catch(() => null);
  const lang = resolveLang(DEFAULT_LANG, before?.edit_request);
  await postPython(`/jobs/${encodeURIComponent(jobId)}/confirm`);
  await sendText(waNumber, t(lang, "已确认，正在剪辑视频...", "Confirmed. Editing video now..."));
  const status = await waitForStatus(jobId,
    ["PREVIEW_READY", "ERROR"],
    Number(env("WA_PIPELINE_TIMEOUT_MS", "900000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Python pipeline failed");
  }
  await sendText(waNumber, previewReadyMessage(lang, jobId, status.degraded_operations) + idleHint(lang, "export"));
  await armIdle(waNumber, jobId, "export", lang); // 进入"等待导出"，重新计时
}

// 整单按原方案重跑：预览有降级步骤（用户要完整效果）或 ERROR 后再试。
// 与 revise 的区别：不改方案，只重执行。
async function retryJob({ waNumber, jobId, text }) {
  await disarmIdle(waNumber);
  const before = await getPythonJob(jobId).catch(() => null);
  const lang = resolveLang(DEFAULT_LANG, text, before?.edit_request);
  await postPython(`/jobs/${encodeURIComponent(jobId)}/retry`);
  await sendText(waNumber, t(lang,
    "正在按原方案重新剪辑...",
    "Retrying the edit with the same plan..."));
  const status = await waitForStatus(jobId,
    ["PREVIEW_READY", "ERROR"],
    Number(env("WA_PIPELINE_TIMEOUT_MS", "900000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Python pipeline failed");
  }
  const jobLang = resolveLang(lang, status.edit_request);
  await sendText(waNumber, previewReadyMessage(jobLang, jobId, status.degraded_operations) + idleHint(jobLang, "export"));
  await armIdle(waNumber, jobId, "export", jobLang);
}

async function renderJob({ waNumber, jobId }) {
  await disarmIdle(waNumber); // 用户已导出，作废"等待导出"的超时
  const before = await getPythonJob(jobId).catch(() => null);
  const lang = resolveLang(DEFAULT_LANG, before?.edit_request);
  await postPython(`/jobs/${encodeURIComponent(jobId)}/render`);
  await sendText(waNumber, t(lang,
    "已开始导出，完成后会把最终视频发给你。",
    "Export started. Will send the final video when ready."));
  const status = await waitForStatus(jobId,
    ["DONE", "ERROR"],
    Number(env("WA_RENDER_TIMEOUT_MS", "900000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Python render failed");
  }

  // 成品通常 > 16MB，超出 WhatsApp 视频消息上限，统一以链接投递（走 PUBLIC_BASE_URL）
  await sendText(waNumber, t(lang,
    `最终视频已生成：${fileUrl(jobId, "final.mp4")}`,
    `Your final video is ready: ${fileUrl(jobId, "final.mp4")}`));
  await redis.del(activeJobKey(waNumber));
}

async function cancelJob({ waNumber, jobId }) {
  await disarmIdle(waNumber); // 作废挂起的超时任务
  const before = await getPythonJob(jobId).catch(() => null);
  const lang = resolveLang(DEFAULT_LANG, before?.edit_request);
  await redis.del(activeJobKey(waNumber));
  await sendText(waNumber, t(lang,
    `已取消任务 ${jobId}。发送新视频即可重新开始。`,
    `Cancelled job ${jobId}. Send a new video when ready.`));
}

async function sendHelp({ waNumber, text }) {
  const lang = resolveLang(DEFAULT_LANG, text);
  await sendText(waNumber, t(lang,
    "发一段视频并配上说明（例如：去掉空白、加中文字幕）。\n" +
    "想加 b-roll？先发主视频、再发每段补充画面（各自配一句“讲到X时放这段”），" +
    "全部发完回复 *go* 开始。",
    "Send a video with an instruction (e.g. \"remove dead air, add subtitles\").\n" +
    "Want b-roll? Send the main video first, then each extra clip with a caption saying where it goes, " +
    "then reply *go* when done."));
}

// 自由文本问答——网关侧收到一条既不是命令、也不在任何活跃任务/收集态里的
// 文字时走这里，取代之前"一律回写死帮助文案"的答非所问。Python 的 /qa
// 端点会真正读懂问题内容、按提问的语言回答；这里只是转发 + 兜底。
async function answerQuestion({ waNumber, text }) {
  try {
    const resp = await axios.post(`${pythonApiBase}/qa`,
      new URLSearchParams({ text: text || "" }).toString(),
      {
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        timeout: Number(env("WA_QA_TIMEOUT_MS", "30000")),
      });
    const answer = resp.data && resp.data.answer;
    if (answer) {
      await safeSendText(waNumber, answer);
      return;
    }
  } catch (err) {
    console.warn(`[worker] /qa failed: ${err.message}`);
  }
  // Python 那边彻底不可用时的最后兜底——仍按语言回一句有用的话，而不是沉默
  const lang = resolveLang(DEFAULT_LANG, text);
  await safeSendText(waNumber, t(lang,
    "发一段视频给我，我会自动剪辑。回复 confirm 确认方案，export 导出成片。也可以直接问我具体问题。",
    "Send me a video and I'll edit it automatically. Reply confirm to approve the plan, " +
    "export for the final cut. Feel free to ask me anything specific."));
}

// ── b-roll 收集态 ──────────────────────────────────────────────
// 每收到一条素材回一句引导；用户回 'go' 收尾。2+ 视频时问哪个是主视频。

async function collectAck({ waNumber, count, kind, caption }) {
  const lang = resolveLang(DEFAULT_LANG, caption);
  const noun = t(lang, kind === "image" ? "图片" : "视频", kind === "image" ? "an image" : "a video");
  const note = caption ? t(lang, `（说明：${caption}）`, ` (note: ${caption})`) : "";
  await safeSendText(waNumber, t(lang,
    `已收到第 ${count} 个${noun}${note}。\n` +
    `可以继续发素材，也可以直接用文字描述（例如“视频1是主视频加字幕；视频2讲到 VS Code 时插入”）。` +
    `全部发完后回复 *go*（或“开始/完成”）即可开始，回复 *cancel* 取消。`,
    `Received item #${count}, ${noun}${note}.\n` +
    `Keep sending more assets, or describe them in text (e.g. "video 1 is the main clip with ` +
    `subtitles; insert video 2 when I mention VS Code"). Reply *go* when done, or *cancel* to clear.`));
}

// 收集期发来的独立文字：存进 notes 缓冲，供 go 时的 LLM 解析用
async function collectNote({ waNumber, text }) {
  const lang = resolveLang(DEFAULT_LANG, text);
  await safeSendText(waNumber, t(lang,
    "已记下你的描述。可继续发素材或补充描述；发完回复 *go* 开始。",
    "Got your note. Keep sending assets or more notes; reply *go* when done."));
}

async function collectNudge({ waNumber, count, text }) {
  const lang = resolveLang(DEFAULT_LANG, text);
  await safeSendText(waNumber, t(lang,
    `已收到 ${count} 个素材。发完后回复 *go* 开始剪辑，回复 *cancel* 清空重来。`,
    `Received ${count} assets so far. Reply *go* when done, or *cancel* to start over.`));
}

async function collectCancel({ waNumber, text, captionSignal }) {
  const lang = resolveLang(DEFAULT_LANG, text, captionSignal);
  await redis.del(notesKey(waNumber));
  await safeSendText(waNumber, t(lang,
    "已清空本次素材与描述。重新发送视频即可开始。",
    "Cleared. Send a new video to start again."));
}

async function finalizeCollection({ waNumber, text }) {
  const items = (await redis.lrange(collectKey(waNumber), 0, -1)).map((s) => JSON.parse(s));
  const videos = items.filter((i) => i.kind === "video");
  const captionSignal = items.map((i) => i.caption).find((c) => c);
  const lang = resolveLang(DEFAULT_LANG, text, captionSignal);
  if (videos.length === 0) {
    await safeSendText(waNumber, t(lang,
      "还没有收到主视频。请先发送一段你要编辑的视频，再回复 *go*。",
      "No main video received yet. Send the video you want edited, then reply *go*."));
    return;
  }
  // 汇总描述：各媒体配文 + 收集期发来的独立文字；交给 Python 的 LLM 解析角色/说明/编辑要求
  const notes = await buildNotes(waNumber, items);
  const assign = await postAssign(videos.length, notes);

  if (videos.length === 1) {
    // 只有一个视频，它就是主，无需询问
    await startWithMain(waNumber, items, videos[0], assign, lang);
    return;
  }
  const mainNum = Number(assign && assign.main_index);
  if (mainNum >= 1 && mainNum <= videos.length) {
    // LLM 从文字判断出了主视频，直接开跑
    await startWithMain(waNumber, items, videos[mainNum - 1], assign, lang);
    return;
  }
  // 判不出主视频 → 保留交互，问编号；把 assign 和这轮判出的语言都存起来，
  // 编号回来后（下一条消息可能只是一个数字，判不出语言）复用。
  await redis.set(assignKey(waNumber), JSON.stringify(assign || {}), "EX", Number(env("WA_COLLECT_TTL", "3600")));
  await redis.set(awaitChoiceKey(waNumber), "1", "EX", Number(env("WA_COLLECT_TTL", "3600")));
  await redis.set(collectLangKey(waNumber), lang, "EX", Number(env("WA_COLLECT_TTL", "3600")));
  const lines = [t(lang,
    "没看出哪个是*主视频*（出镜/口播那条）。可以：回复编号选一个，或再用一句话补充说明（例如“视频1是主视频，视频2讲到 VS Code 时插入”），我据此安排；其余作为 b-roll：",
    "Couldn't tell which one is the *main video* (the talking-head clip). Reply with a number, " +
    "or describe it in a sentence (e.g. \"video 1 is the main one, insert video 2 when I mention VS Code\"); " +
    "the rest become b-roll:")];
  videos.forEach((v, i) => lines.push(`${i + 1}. ${t(lang, "视频", "Video")}${v.caption ? ` — ${v.caption}` : ""}`));
  await safeSendText(waNumber, lines.join("\n"));
}

async function collectionChoice({ waNumber, choice }) {
  const items = (await redis.lrange(collectKey(waNumber), 0, -1)).map((s) => JSON.parse(s));
  const videos = items.filter((i) => i.kind === "video");
  const rememberedLang = await redis.get(collectLangKey(waNumber));
  const lang = resolveLang(rememberedLang || DEFAULT_LANG, choice);
  if (videos.length === 0) {
    // 素材已过期/被清空 → 退出选择态，别把用户卡住
    await redis.del(awaitChoiceKey(waNumber));
    await redis.del(assignKey(waNumber));
    await safeSendText(waNumber, t(lang,
      "素材好像已经过期或清空了，请重新发送视频后再回复 *go*。",
      "Assets seem to have expired or been cleared. Resend the video, then reply *go*."));
    return;
  }
  const text = String(choice || "").trim();
  const isBareNumber = /^\s*\d+\s*$/.test(text);

  // 情况一：只回了一个编号 → 直接选主视频（沿用之前缓存的 labels/edit_request）
  if (isBareNumber) {
    const n = parseInt(text, 10);
    if (n < 1 || n > videos.length) {
      await safeSendText(waNumber, t(lang,
        `请回复 1-${videos.length} 之间的编号，选择主视频。`,
        `Please reply with a number from 1-${videos.length} to choose the main video.`));
      return;
    }
    let assign = { main_index: n, labels: {}, edit_request: "" };
    const raw = await redis.get(assignKey(waNumber));
    if (raw) { try { assign = JSON.parse(raw); } catch (e) { /* 用默认 */ } }
    assign.main_index = n;  // 用户手选的编号优先于 LLM 的判断
    await redis.del(awaitChoiceKey(waNumber));
    await redis.del(assignKey(waNumber));
    await redis.del(collectLangKey(waNumber));
    await startWithMain(waNumber, items, videos[n - 1], assign, lang);
    return;
  }

  // 情况二：发的是一段文字说明 → 存进 notes，重新用 LLM 解析角色/说明/编辑要求
  // （修复：以前这里只认编号，用户在“问主视频”阶段发的描述会被拒绝、丢失）
  await redis.rpush(notesKey(waNumber), text);
  await redis.expire(notesKey(waNumber), Number(env("WA_COLLECT_TTL", "3600")));
  const notes = await buildNotes(waNumber, items);
  const assign = await postAssign(videos.length, notes);
  const textLang = resolveLang(lang, text);  // 这次的文字说明比之前的编号更能判断语言
  const mainNum = Number(assign && assign.main_index);
  if (mainNum >= 1 && mainNum <= videos.length) {
    // 这次文字里能判出主视频 → 直接开跑
    await redis.del(awaitChoiceKey(waNumber));
    await redis.del(assignKey(waNumber));
    await redis.del(collectLangKey(waNumber));
    await startWithMain(waNumber, items, videos[mainNum - 1], assign, textLang);
    return;
  }
  // 仍判不出主视频 → 缓存这次解析出的 labels/edit_request + 语言，继续问编号
  await redis.set(assignKey(waNumber), JSON.stringify(assign || {}), "EX", Number(env("WA_COLLECT_TTL", "3600")));
  await redis.set(collectLangKey(waNumber), textLang, "EX", Number(env("WA_COLLECT_TTL", "3600")));
  const lines = [t(textLang,
    "收到你的说明。还差一步：哪个是*主视频*（出镜/口播那条）？回一个编号即可，其余作为 b-roll：",
    "Got your description. One more step: which is the *main video* (the talking-head clip)? " +
    "Reply with a number; the rest become b-roll:")];
  videos.forEach((v, i) => lines.push(`${i + 1}. ${t(textLang, "视频", "Video")}${v.caption ? ` — ${v.caption}` : ""}`));
  await safeSendText(waNumber, lines.join("\n"));
}

// 用 assign 的 label/edit_request 组装 b-roll 列表并开跑
async function startWithMain(waNumber, items, mainItem, assign, lang) {
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
  await runCollectionJob(waNumber, mainItem, brollItems, editRequest, lang);
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
  notes.forEach((n) => parts.push(n));
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
async function runCollectionJob(waNumber, mainItem, brollItems, editRequest, lang) {
  const effLang = resolveLang(lang || DEFAULT_LANG, editRequest, mainItem.caption);
  if (!hasWACredentials()) {
    await sendText(waNumber, t(effLang,
      "服务正在启动，请稍后再试。",
      "Service is starting up. Please try again in a moment."));
    throw new Error("WhatsApp credentials not configured");
  }
  // 原子认领：DEL 返回被删键数。两条 'go'/两次编号并发时只有一个删到（返回 1），
  // 其余返回 0 直接退出，避免重复建任务（对抗性审查 #4）。
  const claimed = await redis.del(collectKey(waNumber));
  if (!claimed) return;
  await redis.del(notesKey(waNumber));  // 清描述缓冲
  const brollNote = brollItems.length
    ? t(effLang, `，并叠加 ${brollItems.length} 段 b-roll`, ` with ${brollItems.length} b-roll clip(s)`)
    : "";
  await sendText(waNumber, t(effLang,
    `开始处理主视频${brollNote}，正在下载素材并生成剪辑方案...`,
    `Processing the main video${brollNote}, downloading assets and generating an edit plan...`));

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
    const jobLang = resolveLang(effLang, status.edit_request);

    if (status.status === "ERROR") {
      throw new Error(status.error_message || "Python planning failed");
    }
    if (status.status === "NEEDS_CLARIFICATION") {
      await sendText(waNumber, clarificationMessage(jobLang, status));
      return;
    }
    if (status.status === "PREVIEW_READY") {
      await sendText(waNumber, previewReadyMessage(jobLang, jobId, status.degraded_operations) + idleHint(jobLang, "export"));
      await armIdle(waNumber, jobId, "export", jobLang);
      return;
    }
    await sendText(waNumber, formatPlanMessage(status, jobLang) + idleHint(jobLang, "confirm"));
    await armIdle(waNumber, jobId, "confirm", jobLang);
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
  const before = await getPythonJob(jobId).catch(() => null);
  const lang = resolveLang(DEFAULT_LANG, text, before?.edit_request);
  await sendText(waNumber, t(lang, "收到修改意见，正在重新规划...", "Got your feedback, revising the plan..."));
  await postPythonForm(`/jobs/${encodeURIComponent(jobId)}/revise`, text);
  const status = await waitForStatus(jobId,
    ["WAITING_CONFIRMATION", "NEEDS_CLARIFICATION", "ERROR"],
    Number(env("WA_PLAN_TIMEOUT_MS", "180000")));
  if (status.status === "ERROR") {
    throw new Error(status.error_message || "Revise failed");
  }
  const jobLang = resolveLang(lang, status.edit_request);
  if (status.status === "NEEDS_CLARIFICATION") {
    const q = status.planned_edit?.clarification_question ||
      t(jobLang, "需要更多信息才能继续。", "I need more information to continue.");
    await sendText(waNumber, q);
    return;
  }
  await sendText(waNumber, formatPlanMessage(status, jobLang) + idleHint(jobLang, "confirm"));
  await armIdle(waNumber, jobId, "confirm", jobLang); // 新方案又回到"等待确认"，重新计时
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

function formatPlanMessage(job, lang) {
  const plan = job.planned_edit || {};
  const resolvedLang = lang || resolveLang(DEFAULT_LANG, plan.summary, job.edit_request);
  const lines = [
    t(resolvedLang, "*视频编辑方案*", "*Video edit plan*"),
    "",
    plan.summary || t(resolvedLang, "编辑方案已生成。", "Edit plan prepared."),
  ];
  const ops = plan.edit_operations || [];
  if (ops.length) {
    lines.push("", t(resolvedLang, "*将执行的操作：*", "*Operations:*"));
    ops.forEach((op, i) => lines.push(`${i + 1}. ${op.description || op.type || t(resolvedLang, "编辑", "Edit")}`));
  }
  lines.push("", t(resolvedLang,
    "回复 *confirm* 开始，或 *cancel* 取消。",
    "Reply *confirm* to start, or *cancel* to stop."));
  return lines.join("\n");
}

function previewReadyMessage(lang, jobId, degradedOps) {
  let msg = t(lang,
    `预览已生成：${fileUrl(jobId, "preview.mp4")}\n回复 export 导出最终视频。`,
    `Preview ready: ${fileUrl(jobId, "preview.mp4")}\nReply export to generate final video.`);
  // 降级必须发声：某步非致命失败被跳过时（Python 侧已自动重试过一次），明确
  // 告诉用户缺了什么、怎么补救——决不静默交付半成品假装全须全尾。
  const ops = degradedOps || [];
  if (ops.length) {
    const labels = {
      apply_style: t(lang, "品牌模板渲染", "branded template render"),
      insert_broll: t(lang, "b-roll 合成", "b-roll compositing"),
    };
    const names = ops.map((o) => labels[o] || o).join(t(lang, "、", ", "));
    msg += t(lang,
      `\n\n⚠️ 注意：「${names}」这一步执行失败（已自动重试过一次），当前预览不含该效果，只包含已成功的步骤。\n回复 *retry* 重跑完整效果，或回复 *export* 接受当前版本。`,
      `\n\n⚠️ Note: the "${names}" step failed (auto-retried once). This preview does not include that effect — ` +
      `only the steps that succeeded.\nReply *retry* to re-run the full edit, or *export* to accept this version.`);
  }
  return msg;
}

function clarificationMessage(lang, status) {
  const q = status.planned_edit?.clarification_question ||
    t(lang, "我需要更多信息才能编辑这段视频。", "I need more details to edit this video.");
  return `${q}\n\n${t(lang,
    "请补充具体细节（例如从第几秒到第几秒），然后重新发送这段视频。",
    "Please add more detail (e.g. which seconds), then resend the video.")}`;
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

// 方案/预览消息后缀：告知多久内不操作会自动取消（双语）
function idleHint(lang, stage) {
  const act = stage === "export" ? "export/cancel" : "confirm/cancel";
  return t(lang,
    `\n（请在 ${idleMins()} 分钟内回复 ${act}，否则将自动取消本次剪辑）`,
    `\n(Reply ${act} within ${idleMins()} min, or this edit auto-cancels)`);
}

// 排定本次等待的超时提醒 + 自动取消。stage: "confirm" | "export"；lang 用于双语提醒
async function armIdle(waNumber, jobId, stage, lang) {
  const gen = await redis.incr(awaitGenKey(waNumber));
  const ttl = Math.ceil(idleTimeoutMs() / 1000) + 120;
  await redis.set(awaitKey(waNumber), JSON.stringify({ jobId, stage, gen, lang }), "EX", ttl);
  await redis.expire(awaitGenKey(waNumber), ttl);
  const data = { waNumber, jobId, stage, gen, lang };
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

async function idleWarn({ waNumber, jobId, stage, gen, lang }) {
  if (!(await _idleStillValid(waNumber, jobId, gen))) return;
  const L = lang || DEFAULT_LANG;
  const act = stage === "export"
    ? t(L, "export（导出）或 cancel（取消）", "export or cancel")
    : t(L, "confirm（确认）或 cancel（取消）", "confirm or cancel");
  await safeSendText(waNumber, t(L,
    `提醒：本次剪辑约 ${idleWarnMins()} 分钟后将因无操作自动取消。回复 ${act} 即可继续。`,
    `Reminder: this edit will auto-cancel in about ${idleWarnMins()} min without action. Reply ${act} to continue.`));
}

async function idleCancel({ waNumber, jobId, stage, gen, lang }) {
  if (!(await _idleStillValid(waNumber, jobId, gen))) return;
  await redis.del(activeJobKey(waNumber));
  await redis.del(awaitKey(waNumber));
  await redis.incr(awaitGenKey(waNumber)); // 再 bump，防止残留延时任务重复触发
  const L = lang || DEFAULT_LANG;
  await safeSendText(waNumber, t(L,
    "本次剪辑因长时间无操作已自动取消。需要的话重新发送视频即可重新开始。",
    "This edit was auto-cancelled after a long idle. Send a new video to start again."));
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

// “选主视频”这轮问答期间判出的语言，跨消息保留——用户下一条回复可能只是
// 一个裸编号，本身没有任何语言信号，得延用上一步判出来的。
function collectLangKey(waNumber) {
  return `wa:user:${waNumber}:collect_lang`;
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
