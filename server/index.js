import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import crypto from "crypto";
import express from "express";
import { Queue } from "bullmq";
import IORedis from "ioredis";
import axios from "axios";
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
    // 必须把客户端的 Range 头转发给 Python，也要把 Python 回的 range 相关响应头
    // 转发回去——不转发的后果不是"慢一点"，是浏览器 <video> 标签直接播放不了：
    // Chrome 对 <video> 发的请求带 Range，服务端如果永远回 200（整个文件、不是
    // 206 Partial Content）会被 Opaque Response Blocking 拦下，表现为
    // MediaPlaybackError，画面直接黑屏、控制台看不出明显原因（Phase 2 编辑器的
    // 真实浏览器联调中直接复现，之前这条代理只被 WhatsApp 自己的播放器/下载链接
    // 用过，从来没有真的被 <video src> 加载过，这个缺口一直没暴露出来）。
    const upstreamRes = await axios.get(upstream, {
      responseType: "stream",
      validateStatus: () => true,
      headers: req.headers.range ? { range: req.headers.range } : {},
    });
    res.status(upstreamRes.status);
    const forwardHeaders = ["content-type", "content-length", "accept-ranges", "content-range", "etag", "last-modified"];
    for (const h of forwardHeaders) {
      if (upstreamRes.headers[h]) res.setHeader(h, upstreamRes.headers[h]);
    }
    upstreamRes.data.pipe(res);
  } catch (err) {
    console.error(`[files-proxy] failed to fetch ${upstream}:`, err.message);
    res.sendStatus(502);
  }
});

// Studio 预览页数据接口：JSON 代理到 Python GET /batches/{id}（跟上面
// /files 那条同一个"薄代理"模式，浏览器不用直连 Python API）。
app.get("/api/batches/:batchId", async (req, res) => {
  const upstream = `${PYTHON_API_BASE}/batches/${encodeURIComponent(req.params.batchId)}`;
  try {
    const upstreamRes = await axios.get(upstream, { validateStatus: () => true });
    res.status(upstreamRes.status).json(upstreamRes.data);
  } catch (err) {
    console.error(`[batches-proxy] failed to fetch ${upstream}:`, err.message);
    res.sendStatus(502);
  }
});

// Studio 预览页本体：纯静态页面 + 客户端 JS 拉 /api/batches/:batchId 渲染
// （跟 /upgrade 同一个"Node 挂静态页、WhatsApp 消息里发链接过去"的模式）。
// batchId 本身不可猜测（uuid 前 12 位）就是这里的访问控制，跟 /files/:jobId
// 一直以来的做法一致，未额外加登录。
app.get("/studio/:batchId", (_req, res) => {
  res.sendFile(resolve(dirname(fileURLToPath(import.meta.url)), "studio.html"));
});

// 网页版 dashboard 建批次：跟 /api/batches 同一个薄代理模式，区别是这条是
// POST 且带文件（照片）——不引入 multer 之类的中间件重新解析一遍 multipart，
// 直接把 Express 请求本身（一个可读流）连同原始 Content-Type（含 multipart
// boundary）转发给 Python，Python 的 FastAPI UploadFile 自己解析。
// express.json() 只处理 application/json，不会消费掉这里的请求体。
app.post("/api/social-batch", async (req, res) => {
  try {
    const upstreamRes = await axios.post(`${PYTHON_API_BASE}/social-batch`, req, {
      headers: { "content-type": req.headers["content-type"] },
      maxBodyLength: Infinity, maxContentLength: Infinity,
      timeout: Number(env("WA_PYTHON_CREATE_TIMEOUT_MS", "180000")),
      validateStatus: () => true,
    });
    res.status(upstreamRes.status).json(upstreamRes.data);
  } catch (err) {
    console.error("[social-batch-proxy] failed:", err.message);
    res.status(502).json({ error: err.message });
  }
});

// 网页版 dashboard 首页 + 付费页——都是纯静态页，跟 /studio 同一个模式。
app.get(["/dashboard", "/"], (_req, res) => {
  res.sendFile(resolve(dirname(fileURLToPath(import.meta.url)), "dashboard.html"));
});
app.get("/upgrade", (_req, res) => {
  res.sendFile(resolve(dirname(fileURLToPath(import.meta.url)), "upgrade.html"));
});

// 发布流程原型用的样例素材（真实生成过的内容，固定文件，不依赖数据库里的
// 某条 job——纯粹给"假设接了 API 之后长什么样"这个原型演示用）。
app.use("/samples", express.static(resolve(dirname(fileURLToPath(import.meta.url)), "samples")));
app.get("/publish-flow-demo", (_req, res) => {
  res.sendFile(resolve(dirname(fileURLToPath(import.meta.url)), "publish-flow-demo.html"));
});

// ---------------------------------------------------------------------------
// Preview editor (Phase 2) — serves the built editor SPA and proxies its API
// calls to the Python service. Everything here is same-origin (this gateway
// is the one publicly tunneled server, see the PYTHON_API_BASE comment
// above), so no CORS setup is needed anywhere in this feature.
// ---------------------------------------------------------------------------

const EDITOR_DIST_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../remotion-composer/editor-dist");

app.use("/editor/assets", express.static(resolve(EDITOR_DIST_DIR, "assets")));

app.get("/editor/:jobId", (req, res) => {
  // Token 鉴权完全交给 API 调用做（前端加载后立刻打 GET /api/editor/:jobId/
  // props，拿到 403 就自己显示错误态）——这里只是静态 SPA 外壳，本身不含
  // 任何敏感信息，不需要在这一层重复校验。
  res.sendFile(resolve(EDITOR_DIST_DIR, "index.html"), (err) => {
    if (err) {
      res.status(404).send("Editor not built yet — run `npm run build:editor` in remotion-composer/");
    }
  });
});

app.get(["/api/editor/:jobId/props", "/api/editor/:jobId/status",
         "/api/editor/:jobId/filmstrip", "/api/editor/:jobId/waveform",
         "/api/editor/:jobId/authored"], async (req, res) => {
  // req.path 已经是 /api/editor/:jobId/props 这类完整路径，去掉 /api 前缀
  // 直接对应 Python 那边的 /editor/:jobId/props 路由。
  const upstream = `${PYTHON_API_BASE}${req.path.replace(/^\/api/, "")}?token=${encodeURIComponent(req.query.token || "")}`;
  try {
    const upstreamRes = await axios.get(upstream, { validateStatus: () => true });
    res.status(upstreamRes.status).json(upstreamRes.data);
  } catch (err) {
    console.error(`[editor-api] GET ${req.path} failed:`, err.message);
    res.sendStatus(502);
  }
});

app.post("/api/editor/:jobId/relayout", async (req, res) => {
  const upstream = `${PYTHON_API_BASE}/editor/${encodeURIComponent(req.params.jobId)}/relayout`
    + `?token=${encodeURIComponent(req.query.token || "")}`;
  try {
    const upstreamRes = await axios.post(upstream, req.body, { validateStatus: () => true });
    res.status(upstreamRes.status).json(upstreamRes.data);
  } catch (err) {
    console.error("[editor-api] relayout failed:", err.message);
    res.sendStatus(502);
  }
});

// 上传自己的背景音乐——跟 /api/social-batch 同一个"原样转发请求流"模式
// （见那条路由自己的注释），不是 /props 那种先解析 JSON 再转发的形状：
// multipart 请求体必须原封不动地带着它的 boundary 一路转发给 Python 的
// UploadFile 自己解析，Express 这层完全不碰它。
app.post("/api/editor/:jobId/music", async (req, res) => {
  const upstream = `${PYTHON_API_BASE}/editor/${encodeURIComponent(req.params.jobId)}/music`
    + `?token=${encodeURIComponent(req.query.token || "")}`;
  try {
    const upstreamRes = await axios.post(upstream, req, {
      headers: { "content-type": req.headers["content-type"] },
      maxBodyLength: Infinity, maxContentLength: Infinity,
      timeout: Number(env("WA_PYTHON_CREATE_TIMEOUT_MS", "180000")),
      validateStatus: () => true,
    });
    res.status(upstreamRes.status).json(upstreamRes.data);
  } catch (err) {
    console.error("[editor-api] music upload failed:", err.message);
    res.sendStatus(502);
  }
});

app.delete("/api/editor/:jobId/music", async (req, res) => {
  const upstream = `${PYTHON_API_BASE}/editor/${encodeURIComponent(req.params.jobId)}/music`
    + `?token=${encodeURIComponent(req.query.token || "")}`;
  try {
    const upstreamRes = await axios.delete(upstream, { validateStatus: () => true });
    res.status(upstreamRes.status).json(upstreamRes.data);
  } catch (err) {
    console.error("[editor-api] music delete failed:", err.message);
    res.sendStatus(502);
  }
});

app.post("/api/editor/:jobId/props", async (req, res) => {
  const jobId = req.params.jobId;
  const upstream = `${PYTHON_API_BASE}/editor/${encodeURIComponent(jobId)}/props`
    + `?token=${encodeURIComponent(req.query.token || "")}`;
  let upstreamRes;
  try {
    upstreamRes = await axios.post(upstream, req.body, { validateStatus: () => true });
  } catch (err) {
    console.error("[editor-api] save failed:", err.message);
    return res.sendStatus(502);
  }
  // Bug fix: Python's POST /editor/{id}/props actually returns 200, not 202
  // (webhook.py never sets status_code=202 anywhere on this route) — the old
  // `!== 202` check therefore always took this early-return branch, which
  // meant (a) the BullMQ "editor-save" enqueue below never ran, so a save's
  // rendered result was never delivered to WhatsApp, and (b) it skipped past
  // the wa_number-stripping code, forwarding the raw upstream body —
  // including wa_number — straight to the browser. See the comment below on
  // exactly why that must never happen.
  if (upstreamRes.status >= 400) {
    return res.status(upstreamRes.status).json(upstreamRes.data);
  }
  // wa_number 只应该在服务器之间传递，绝不能原样透传回浏览器——这条编辑器
  // 链接谁点开都能保存，发起保存的人不一定就是那个 WhatsApp 号码本人；
  // wa_number 只用来把这次保存接进 BullMQ 投递队列，触发 worker.js 的
  // editorSave 把渲染结果发回真正的 WhatsApp 对话。
  const { wa_number: waNumber, state, job_id: returnedJobId } = upstreamRes.data || {};
  const effectiveJobId = returnedJobId || jobId;
  // state === "coalesced" 意味着已经有一次保存正在渲染中，这次只是把
  // pending_props 换成了最新版本——那次已经入队的 editor-save 完成后投递
  // 的就是这份最新内容，这里再入队一次只会造成两条重复的 WhatsApp 消息。
  if (waNumber && state !== "coalesced") {
    const msgId = `editor-${effectiveJobId}-${Date.now()}`;
    try {
      await videoQueue.add("editor-save", { waNumber, jobId: effectiveJobId, msgId }, queueOptions(msgId));
    } catch (err) {
      console.error("[editor-api] failed to enqueue editor-save:", err.message);
    }
  } else if (!waNumber) {
    console.warn(`[editor-api] save for ${effectiveJobId} had no wa_number — delivery will not fire`);
  }
  res.status(upstreamRes.status).json({ job_id: effectiveJobId, state });
});

// Phase 8 — Arm B (AI-authored) manual edits. Mirrors the /props POST
// handler above exactly (same upstream-status check, same wa_number →
// BullMQ "editor-save" enqueue) — a job is only ever Arm A or Arm B, never
// both, but the delivery mechanism once a save lands is identical either
// way, so this deliberately isn't a new code path, just a new upstream path.
app.post("/api/editor/:jobId/overrides", async (req, res) => {
  const jobId = req.params.jobId;
  const upstream = `${PYTHON_API_BASE}/editor/${encodeURIComponent(jobId)}/overrides`
    + `?token=${encodeURIComponent(req.query.token || "")}`;
  let upstreamRes;
  try {
    upstreamRes = await axios.post(upstream, req.body, { validateStatus: () => true });
  } catch (err) {
    console.error("[editor-api] authored save failed:", err.message);
    return res.sendStatus(502);
  }
  // See the matching comment on the /props handler above — same 200-vs-202
  // bug, same wa_number leak, same fix.
  if (upstreamRes.status >= 400) {
    return res.status(upstreamRes.status).json(upstreamRes.data);
  }
  const { wa_number: waNumber, state, job_id: returnedJobId } = upstreamRes.data || {};
  const effectiveJobId = returnedJobId || jobId;
  if (waNumber && state !== "coalesced") {
    const msgId = `editor-${effectiveJobId}-${Date.now()}`;
    try {
      await videoQueue.add("editor-save", { waNumber, jobId: effectiveJobId, msgId }, queueOptions(msgId));
    } catch (err) {
      console.error("[editor-api] failed to enqueue editor-save:", err.message);
    }
  } else if (!waNumber) {
    console.warn(`[editor-api] authored save for ${effectiveJobId} had no wa_number — delivery will not fire`);
  }
  res.status(upstreamRes.status).json({ job_id: effectiveJobId, state });
});

// Dashboard "Edit" action — mints a token via Python (POST /jobs/:id/editor_token,
// itself already scoped "给 Node 网关用") and re-hosts the URL onto whatever
// origin the browser actually loaded the dashboard from. Python's own
// public_base_url defaults to its own port (localhost:8000) and isn't
// guaranteed to match the gateway's address in every environment (local dev
// vs. an ngrok tunnel), whereas req.get("host") always is — the editor SPA
// itself is only ever served from this gateway (see EDITOR_DIST_DIR above),
// never from Python directly.
app.post("/api/jobs/:jobId/editor-link", async (req, res) => {
  const jobId = req.params.jobId;
  const upstream = `${PYTHON_API_BASE}/jobs/${encodeURIComponent(jobId)}/editor_token`;
  let upstreamRes;
  try {
    upstreamRes = await axios.post(upstream, {}, { validateStatus: () => true });
  } catch (err) {
    console.error("[editor-link] mint failed:", err.message);
    return res.sendStatus(502);
  }
  if (upstreamRes.status !== 200) {
    return res.status(upstreamRes.status).json(upstreamRes.data);
  }
  let token;
  try {
    token = new URL(upstreamRes.data.editor_url).searchParams.get("token");
  } catch (err) {
    return res.sendStatus(502);
  }
  if (!token) return res.sendStatus(502);
  const editorUrl = `${req.protocol}://${req.get("host")}/editor/${encodeURIComponent(jobId)}?token=${encodeURIComponent(token)}`;
  res.json({ editor_url: editorUrl });
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
  const msgType = message.type;
  const text = message.text?.body?.trim() || "";

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

  // 交互按钮回复：方案A选臂。按钮只是文字指令的快捷方式，命中就走跟对应
  // 文字指令完全相同的队列任务，不新增后端逻辑；等待状态已过期/不存在时
  // 静默忽略（不报错也不追问），避免用户点了一条陈旧消息上的按钮却卡住。
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

// 方案A(选臂):Redis key 必须跟 worker.js 里的 awaitArmKey 用同一套命名/编码,
// 两个进程各自独立定义(索引进程收消息判断要不要拦下来转发,worker 进程消费)。
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