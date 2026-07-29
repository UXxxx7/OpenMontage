import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import crypto from "crypto";
import fs from "fs";
import os from "os";
import express from "express";
import { Queue } from "bullmq";
import IORedis from "ioredis";
import axios from "axios";
import FormData from "form-data";
import { resolveLang, t } from "./lang.js";

config({ path: resolve(dirname(fileURLToPath(import.meta.url)), "../.env") });

const REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0");
// worker.js builds download links as `${PUBLIC_BASE_URL}/files/{jobId}/{filename}`
// (see fileUrl() there) — PUBLIC_BASE_URL is this gateway's public tunnel URL,
// not the Python API's. The Python API (port 8000, not itself tunneled) is
// what actually serves `/files/...`. Proxy it through here so the one tunnel
// covers both the webhook and file downloads sent back to WhatsApp users —
// without this, links sent to users 404 ("Cannot GET") since this Express
// app had no route for the path at all.
const PYTHON_API_BASE = env("OPENMONTAGE_API_BASE", "http://localhost:8000").replace(/\/$/, "");

function createRedis(name) {
  const client = new IORedis(REDIS_URL, {
    maxRetriesPerRequest: null,
    connectTimeout: Number(env("WA_REDIS_CONNECT_TIMEOUT_MS", "5000")),
    retryStrategy(times) {
      if (times > 10) return null;
      return Math.min(times * 200, 3000);
    },
    lazyConnect: false,
  });
  client.on("error", (err) => {
    console.error(`[redis:${name}] ${err.message}`);
  });
  return client;
}

const redis = createRedis("webhook");
const videoQueue = new Queue(env("WA_QUEUE_NAME", "openmontage-video-jobs"), {
  connection: redis,
});

const app = express();
app.use(express.json({
  limit: env("WA_WEBHOOK_BODY_LIMIT", "10mb"),
  verify: (req, _res, buf) => { req.rawBody = Buffer.from(buf); },
}));

app.get("/health", async (_req, res) => {
  let redisStatus = "connected";
  try {
    await redis.ping();
  } catch (err) {
    redisStatus = err.message;
  }
  res.json({ status: "ok", redis: redisStatus });
});

app.get("/privacy", (_req, res) => {
  res.type("html").send(`<!doctype html>
<html><head><meta charset="utf-8"><title>Privacy Policy</title></head>
<body style="font-family:sans-serif;max-width:680px;margin:40px auto;padding:0 20px;line-height:1.5">
<h1>Privacy Policy</h1>
<p>OpenMontage WhatsApp Gateway receives WhatsApp messages and videos only to process the video editing request you send.</p>
<p>Input media, generated previews, and final renders are retained temporarily for job processing and delivery.</p>
<p>Contact the service owner to request deletion of your submitted media and generated outputs.</p>
</body></html>`);
});

app.get("/files/:jobId/:filename", async (req, res) => {
  const upstream = `${PYTHON_API_BASE}/files/${encodeURIComponent(req.params.jobId)}/${encodeURIComponent(req.params.filename)}`;
  try {
    const upstreamRes = await axios.get(upstream, { responseType: "stream", validateStatus: () => true });
    res.status(upstreamRes.status);
    if (upstreamRes.headers["content-type"]) res.setHeader("content-type", upstreamRes.headers["content-type"]);
    if (upstreamRes.headers["content-length"]) res.setHeader("content-length", upstreamRes.headers["content-length"]);
    upstreamRes.data.pipe(res);
  } catch (err) {
    console.error(`[files-proxy] failed to fetch ${upstream}:`, err.message);
    res.sendStatus(502);
  }
});

app.get(["/webhook", "/webhook/whatsapp"], (req, res) => {
  const mode = req.query["hub.mode"];
  const token = req.query["hub.verify_token"];
  const challenge = req.query["hub.challenge"];
  if (mode === "subscribe" && token === whatsappVerifyToken()) {
    console.log("[webhook] verified");
    return res.status(200).send(challenge);
  }
  return res.sendStatus(403);
});

app.post(["/webhook", "/webhook/whatsapp"], async (req, res) => {
  if (!verifySignature(req)) {
    return res.sendStatus(401);
  }
  const messages = extractMessages(req.body);
  // 先回 200 再处理（Meta 官方要求快速 ACK）：此前是处理完才返回，处理链里
  // 有 Graph API 直发回执等慢操作，一旦超过 Meta 的等待窗口，这次投递会被
  // 记为失败进重试队列——之后带着延迟补投回来，变成"幽灵消息"（2026-07-14
  // 实测事故：换隧道空窗期滞留的旧视频事件在 4 分钟后补投，开出了并行重复任务）。
  res.sendStatus(200);
  for (const message of messages) {
    try {
      await handleMessage(message);
    } catch (err) {
      console.error("[webhook] handleMessage error:", err.message);
    }
  }
});

async function handleMessage(message) {
  const waNumber = message.from;
  const msgId = message.id;
  // msgType/text 用 let，不用 const——语音消息转写完之后会把这两个变量
  // 改写成 ("text", 转写文字)，直接落进下面已有的整套文字路由逻辑
  // （收集态/等选臂/活跃任务确认/修改意见/问答……），不用为语音另外
  // 写一份可能悄悄跟文字路由分叉的平行逻辑。
  let msgType = message.type;
  let text = message.text?.body?.trim() || "";

  if (!waNumber || !msgId) return;

  const seen = await withTimeout(
    markMessageSeen(msgId),
    Number(env("WA_DEDUP_TIMEOUT_MS", "3000")),
    false
  );
  if (!seen) {
    console.log(`[webhook] duplicate or timeout: ${msgId}`);
    return;
  }

  // 陈旧消息过滤：Meta 对投递失败的事件会排队重试（可长达数天），且补投
  // 不保证沿用原消息 ID——仅靠 msgId 去重挡不住。隧道换址/服务重启的空窗
  // 期滞留的旧消息，会在恢复后成批补投进来：几小时前的"发视频"现在才到，
  // 用户视角就是机器人无缘无故自己开新单（2026-07-14 实测事故）。消息自带
  // 用户发送时刻的 timestamp（epoch 秒），超龄直接丢弃。
  const sentAt = Number(message.timestamp || 0);
  const maxAgeS = Number(env("WA_MAX_MESSAGE_AGE_S", "600"));
  if (sentAt && maxAgeS > 0) {
    const ageS = Math.round(Date.now() / 1000 - sentAt);
    if (ageS > maxAgeS) {
      console.log(`[webhook] stale message dropped: ${msgId} age=${ageS}s type=${msgType}`);
      return;
    }
  }

  console.log(`[webhook] from=${waNumber} type=${msgType}`);

  // 语音消息：下载 + 转写成文字，然后原地把这条消息"变成"一条文字消息，
  // 落进下面已有的整套文字路由逻辑——同一句话不管是打字还是说出来，理解
  // 和路由方式完全一样。转写失败/没听清就按"没听清"礼貌回复，不当成
  // 静默失败晾着用户（架构复审后新增，2026-07-29）。
  if (msgType === "audio") {
    const mediaId = message.audio?.id;
    const lang = resolveLang(env("WA_DEFAULT_LANG", "zh"));
    if (!mediaId) return;
    let transcribed = "";
    try {
      transcribed = await downloadAndTranscribeVoice(mediaId);
    } catch (err) {
      console.warn(`[webhook] voice transcribe failed: ${err.message}`);
    }
    if (!transcribed) {
      await gatewaySendText(waNumber, t(lang,
        "抱歉，没听清这条语音消息，可以再说一遍或者直接打字。",
        "Sorry, I couldn't make out that voice message — try again or type it instead."));
      return;
    }
    console.log(`[webhook] voice transcribed: "${transcribed}"`);
    msgType = "text";
    text = transcribed;
  }

  // 方案A:交互按钮回复(选臂)。仅在"待选臂"时有意义,否则忽略。
  if (msgType === "interactive") {
    const btnId = message.interactive?.button_reply?.id || message.interactive?.list_reply?.id || "";
    const awaitArm = await withTimeout(
      redis.get(awaitArmKey(waNumber)), Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")), null);
    if (awaitArm) {
      await videoQueue.add("arm-choice", { waNumber, armId: btnId, msgId }, queueOptions(msgId));
    }
    return;
  }

  // C-roll：一张照片 + 触发词 caption → AI 看图写文案 + HeyGen 生成数字人
  // 说话视频，再自动接入常规剪辑管线。必须显式触发词才认（不能让所有
  // 图片上传都被当成 C-roll 请求）——不然会跟下面正常的 b-roll 图片收集
  // 撞车。且只在完全没有素材收集在途时才认，收集途中发的图（哪怕碰巧
  // 带了这几个词）一律走原来的"当作素材"逻辑，避免打断用户正在做的事。
  const CROLL_TRIGGER_RE = /\b(c[\s-]?roll|croll)\b|数字人|生成视频|ai\s*视频|口播视频|说话视频/i;
  if (msgType === "image") {
    const media = message.image || {};
    const mediaId = media.id;
    const caption = media.caption || "";
    if (mediaId && CROLL_TRIGGER_RE.test(caption)) {
      const pendingCount = await withTimeout(
        redis.llen(collectKey(waNumber)), Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")), 0
      );
      const awaitingChoice = await withTimeout(
        redis.get(awaitChoiceKey(waNumber)), Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")), null
      );
      if (!pendingCount && !awaitingChoice) {
        const lang = resolveLang(env("WA_DEFAULT_LANG", "zh"), caption);
        await gatewaySendText(waNumber, t(lang,
          "收到图片，正在生成数字人说话视频（AI 看图写文案 + 生成口型动画），这一步通常要 1-2 分钟…",
          "Got the photo — generating your talking-photo video (AI writing the script + animating it), usually takes 1-2 minutes…"));
        await videoQueue.add("croll-generate", { waNumber, mediaId, caption, msgId }, queueOptions(msgId));
        return;
      }
    }
  }

  // 视频/图片 → 收集态：每条素材攒进 Redis，等用户回 'go' 再统一建任务
  // （支持“主视频 + 多段 b-roll”）。每收一条回一句引导，告诉用户何时结束。
  if (msgType === "video" || msgType === "image") {
    const media = message[msgType] || {};
    const mediaId = media.id;
    if (!mediaId) return;
    const caption = media.caption || "";
    const count = await redis.rpush(collectKey(waNumber),
      JSON.stringify({ mediaId, caption, kind: msgType }));
    await redis.expire(collectKey(waNumber), Number(env("WA_COLLECT_TTL", "3600")));
    await redis.del(awaitChoiceKey(waNumber)); // 又来新素材 → 作废旧的“选主视频”问题
    // 收集回执由网关直发而不是入队（collect-ack 作为队列任务会排在重活后面，
    // 队列拥堵时回执又变回沉默——2026-07-09 实测过的坑），顺带告知拥堵度。
    let ahead = 0;
    try {
      ahead = (await videoQueue.getWaitingCount()) + (await videoQueue.getActiveCount());
    } catch {}
    const lang = resolveLang(env("WA_DEFAULT_LANG", "zh"), caption);
    const noun = t(lang, msgType === "image" ? "图片" : "视频", msgType === "image" ? "an image" : "a video");
    const note = caption ? t(lang, `（说明：${caption}）`, ` (note: ${caption})`) : "";
    const queueNote = ahead > 0
      ? t(lang, `\n（当前有 ${ahead} 个任务在处理/排队，开始后需要多等一会）`,
        `\n(${ahead} job(s) currently processing/queued — it'll take a bit longer once started)`)
      : "";
    await gatewaySendText(waNumber, t(lang,
      `已收到第 ${count} 个${noun}${note}。\n` +
      `可以继续发素材，也可以用文字补充说明。全部发完回复 *go* 开始，回复 *cancel* 取消。${queueNote}`,
      `Received item #${count}, ${noun}${note}.\n` +
      `Keep sending more assets, or add a text description. Reply *go* when done, or *cancel* to clear.${queueNote}`));
    return;
  }

  if (msgType === "text" && text) {
    const normalized = text.toLowerCase();
    // 方案A:正在等用户选臂时,任何文字都转给 arm-choice(worker 里映射 1/2/a/b/取消)
    const awaitArmFlag = await withTimeout(
      redis.get(awaitArmKey(waNumber)), Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")), null);
    if (awaitArmFlag) {
      await videoQueue.add("arm-choice", { waNumber, armText: text, msgId }, queueOptions(msgId));
      return;
    }
    const activeJobId = await withTimeout(
      redis.get(activeJobKey(waNumber)),
      Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")),
      null
    );

    // ── 收集流程优先 ──
    // 用户刚发过素材（缓冲非空）或正处在“选主视频”阶段时，意图是开一个新任务，
    // 这些分支要压过下面对旧活跃任务的处理，且不受 activeJob 影响（否则残留的
    // 活跃任务会让新素材永远无法用 go 收尾 —— 见对抗性审查 #3/#5）。
    const awaitingChoice = await withTimeout(
      redis.get(awaitChoiceKey(waNumber)),
      Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")),
      null
    );
    const pendingCount = await withTimeout(
      redis.llen(collectKey(waNumber)),
      Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")),
      0
    );
    const cancelWords = ["cancel", "no", "stop", "取消"];
    const goWords = ["go", "start", "done", "开始", "完成", "好了"];

    if (awaitingChoice || pendingCount > 0) {
      // 任何阶段都允许取消（含“选主视频”阶段 —— 修 #5）
      if (cancelWords.includes(normalized)) {
        // "cancel" 本身是裸指令词，不带语言信息（resolveLang 设计上会正确
        // 跳过它）——真正的信号在已收集素材的配文里，但 collectKey 这行
        // 删完 worker 那边就再也读不到了，必须在删除前把配文取出来一起
        // 传过去，不然只能退回默认语言（2026-07-15 实测：英文配文传视频后
        // 回 cancel，收到的是中文回执）。
        let captionSignal;
        try {
          const items = (await redis.lrange(collectKey(waNumber), 0, -1)).map((s) => JSON.parse(s));
          captionSignal = items.map((i) => i.caption).find((c) => c);
        } catch {}
        await redis.del(collectKey(waNumber));
        await redis.del(awaitChoiceKey(waNumber));
        await videoQueue.add("collect-cancel", { waNumber, text, captionSignal, msgId }, queueOptions(msgId));
        return;
      }
      // 选主视频阶段：期待一个编号，交给 worker 校验并建任务
      if (awaitingChoice) {
        await videoQueue.add("collection-choice",
          { waNumber, choice: text, msgId }, queueOptions(msgId));
        return;
      }
      // 'go' 收尾：把缓冲里的素材定角色并开始
      if (goWords.includes(normalized)) {
        await videoQueue.add("finalize-collection", { waNumber, text, msgId }, queueOptions(msgId));
        return;
      }
      // 收集态里发了其它文字 → 当作对素材的描述，存进 notes 缓冲（go 时交给 LLM 解析）
      await redis.rpush(notesKey(waNumber), text);
      await redis.expire(notesKey(waNumber), Number(env("WA_COLLECT_TTL", "3600")));
      await videoQueue.add("collect-note", { waNumber, text, msgId }, queueOptions(msgId));
      return;
    }

    if (activeJobId && ["confirm", "continue", "yes", "ok", "go", "start", "done", "开始", "完成", "好了", "继续"].includes(normalized)) {
      await videoQueue.add("confirm-job", { waNumber, jobId: activeJobId, msgId }, queueOptions(msgId));
      return;
    }
    if (activeJobId && ["export", "final", "render"].includes(normalized)) {
      await videoQueue.add("render-job", { waNumber, jobId: activeJobId, msgId }, queueOptions(msgId));
      return;
    }
    // 按原方案整单重跑——预览有降级步骤（消息里已提示可 retry）或想再试一次
    if (activeJobId && ["retry", "重试"].includes(normalized)) {
      await videoQueue.add("retry-job", { waNumber, jobId: activeJobId, text, msgId }, queueOptions(msgId));
      return;
    }
    if (activeJobId && ["cancel", "no", "stop"].includes(normalized)) {
      await redis.del(activeJobKey(waNumber));
      await videoQueue.add("cancel-job", { waNumber, jobId: activeJobId, msgId }, queueOptions(msgId));
      return;
    }
    // 有活跃任务 + 非命令文本 → 视为对当前方案/预览的修改意见，就地重规划
    if (activeJobId) {
      await videoQueue.add("revise-job", { waNumber, jobId: activeJobId, text, msgId }, queueOptions(msgId));
      return;
    }
    // 没有活跃任务、没在收集态 → 要么是要帮助文案的裸关键词，要么是一句
    // 真正的问题。之前不分青红皂白一律回写死帮助文案，答非所问（用户原
    // 话："不能做到用户问什么回答什么"）。现在只有裸关键词才走廉价的
    // send-help，其余一律转发给真正读懂问题的 answer-question。
    const helpWords = ["help", "帮助", "?", "？", "怎么用", "怎么玩"];
    if (helpWords.includes(normalized)) {
      await videoQueue.add("send-help", { waNumber, text, msgId }, queueOptions(msgId));
      return;
    }
    await videoQueue.add("answer-question", { waNumber, text, msgId }, queueOptions(msgId));
  }
}

function verifySignature(req) {
  const appSecret = whatsappAppSecret();
  if (!appSecret) {
    console.warn("[webhook] WA_APP_SECRET not set; signature verification skipped");
    return true;
  }
  const signature = req.get("x-hub-signature-256") || "";
  const rawBody = req.rawBody || Buffer.from(JSON.stringify(req.body));
  const expected = "sha256=" + crypto.createHmac("sha256", appSecret).update(rawBody).digest("hex");
  const sigBuf = Buffer.from(signature);
  const expBuf = Buffer.from(expected);
  return sigBuf.length === expBuf.length && crypto.timingSafeEqual(sigBuf, expBuf);
}

function extractMessages(body) {
  const out = [];
  for (const entry of body?.entry || [])
    for (const change of entry?.changes || [])
      for (const message of change?.value?.messages || [])
        out.push(message);
  return out;
}

async function markMessageSeen(msgId) {
  const key = `wa:message:${msgId}`;
  const inserted = await redis.set(key, "1", "EX",
    Number(env("WA_MESSAGE_DEDUP_TTL", "86400")), "NX");
  return inserted === "OK";
}

function queueOptions(msgId) {
  return {
    jobId: msgId,
    attempts: Number(env("WA_QUEUE_ATTEMPTS", "2")),
    backoff: { type: "exponential", delay: Number(env("WA_QUEUE_BACKOFF_MS", "5000")) },
    removeOnComplete: Number(env("WA_QUEUE_KEEP_COMPLETE", "100")),
    removeOnFail: Number(env("WA_QUEUE_KEEP_FAILED", "100")),
  };
}

function activeJobKey(waNumber) {
  return `wa:user:${waNumber}:active_job`;
}

// b-roll 收集缓冲：一个用户在按 'go' 之前发来的所有素材（LIST），
// 以及“2+ 视频时正在等用户回主视频编号”的标志。
function collectKey(waNumber) {
  return `wa:user:${waNumber}:collect`;
}

function awaitChoiceKey(waNumber) {
  return `wa:user:${waNumber}:await_choice`;
}

function awaitArmKey(waNumber) {
  return `wa:user:${waNumber}:await_arm`;
}

function notesKey(waNumber) {
  return `wa:user:${waNumber}:notes`;
}

async function withTimeout(promise, ms, fallback) {
  if (ms <= 0) return await promise;
  const timer = new Promise((r) => setTimeout(() => r(fallback), ms));
  return Promise.race([promise, timer]);
}

function env(name, fallback = "") {
  return process.env[name] || fallback;
}

// 下载一条 WhatsApp 语音消息 + 转写成文字（架构复审后新增，2026-07-29）。
// 语音消息通常几秒到一两分钟，直接留在内存里传给 Python 的 /transcribe，
// 不落临时文件——不像 worker.js 那边处理的视频/图片素材，没有"文件可能
// 很大、要流式落盘"的顾虑，也就不需要那边那一整套临时文件生命周期管理。
async function downloadAndTranscribeVoice(mediaId) {
  const token = env("WA_TOKEN", env("WHATSAPP_ACCESS_TOKEN"));
  const info = await axios.get(
    `https://graph.facebook.com/${env("WA_GRAPH_VERSION", "v21.0")}/${mediaId}`,
    { headers: { Authorization: `Bearer ${token}` }, timeout: Number(env("WA_MEDIA_INFO_TIMEOUT_MS", "30000")) }
  );
  const mediaUrl = info.data.url;
  if (!mediaUrl) throw new Error(`WhatsApp voice media ${mediaId} no download URL`);
  const mime = info.data.mime_type || "";
  // WhatsApp 语音消息固定是 audio/ogg; codecs=opus；万一遇到非语音的普通
  // 音频附件（mime 不同），扩展名跟着 mime 走，Python 那边靠 ffmpeg 解码，
  // 不挑格式。
  const ext = mime.includes("ogg") ? "ogg" : mime.includes("mp4") || mime.includes("m4a") ? "m4a" : "ogg";
  const audioResp = await axios.get(mediaUrl, {
    headers: { Authorization: `Bearer ${token}` },
    responseType: "arraybuffer",
    timeout: Number(env("WA_MEDIA_DOWNLOAD_TIMEOUT_MS", "60000")),
  });
  const form = new FormData();
  form.append("audio", Buffer.from(audioResp.data), { filename: `voice.${ext}`, contentType: mime || "audio/ogg" });
  const resp = await axios.post(`${PYTHON_API_BASE}/transcribe`, form, {
    headers: form.getHeaders(),
    maxBodyLength: Infinity, maxContentLength: Infinity,
    timeout: Number(env("WA_TRANSCRIBE_TIMEOUT_MS", "60000")),
  });
  return (resp.data?.text || "").trim();
}

async function gatewaySendText(waNumber, text) {
  const token = env("WA_TOKEN", env("WHATSAPP_ACCESS_TOKEN"));
  const phoneId = env("WA_PHONE_ID", env("WHATSAPP_PHONE_NUMBER_ID"));
  if (!token || !phoneId) return;
  try {
    await axios.post(
      `https://graph.facebook.com/${env("WA_GRAPH_VERSION", "v21.0")}/${phoneId}/messages`,
      { messaging_product: "whatsapp", to: waNumber, type: "text", text: { body: text } },
      { headers: { Authorization: `Bearer ${token}` }, timeout: 10000 }
    );
  } catch (err) {
    console.warn("[gateway] ack send failed:", err.message);
  }
}

function whatsappVerifyToken() {
  return env("WA_VERIFY_TOKEN", env("WHATSAPP_VERIFY_TOKEN"));
}

function whatsappAppSecret() {
  return env("WA_APP_SECRET", env("WHATSAPP_APP_SECRET"));
}

const port = Number(env("WA_GATEWAY_PORT", env("PORT", "3000")));

async function start() {
  try {
    await redis.ping();
    console.log("[gateway] Redis connected");
  } catch (err) {
    console.error("[gateway] Redis not reachable:", err.message);
    console.error("[gateway] Webhook will start but writable ops will fail.");
  }
  app.listen(port, () => {
    console.log(`[gateway] WhatsApp webhook listening on :${port}`);
  });
}

start();