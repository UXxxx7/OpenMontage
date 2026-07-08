import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import crypto from "crypto";
import express from "express";
import { Queue } from "bullmq";
import IORedis from "ioredis";
import axios from "axios";

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
  return new IORedis(REDIS_URL, {
    maxRetriesPerRequest: null,
    connectTimeout: Number(env("WA_REDIS_CONNECT_TIMEOUT_MS", "5000")),
    retryStrategy(times) {
      if (times > 10) return null;
      return Math.min(times * 200, 3000);
    },
    lazyConnect: false,
  });
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
  for (const message of messages) {
    try {
      await handleMessage(message);
    } catch (err) {
      console.error("[webhook] handleMessage error:", err.message);
    }
  }
  return res.sendStatus(200);
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

  console.log(`[webhook] from=${waNumber} type=${msgType}`);

  if (msgType === "video") {
    const mediaId = message.video?.id;
    const caption = message.video?.caption || "";
    if (!mediaId) return;
    await videoQueue.add("edit-video", {
      waNumber, mediaId, editRequest: caption, msgId,
    }, queueOptions(msgId));
    return;
  }

  if (msgType === "text" && text) {
    const normalized = text.toLowerCase();
    const activeJobId = await withTimeout(
      redis.get(activeJobKey(waNumber)),
      Number(env("WA_REDIS_OP_TIMEOUT_MS", "2000")),
      null
    );

    if (activeJobId && ["confirm", "continue", "yes", "ok"].includes(normalized)) {
      await videoQueue.add("confirm-job", { waNumber, jobId: activeJobId, msgId }, queueOptions(msgId));
      return;
    }
    if (activeJobId && ["export", "final", "render"].includes(normalized)) {
      await videoQueue.add("render-job", { waNumber, jobId: activeJobId, msgId }, queueOptions(msgId));
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
    await videoQueue.add("send-help", { waNumber, msgId }, queueOptions(msgId));
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

async function withTimeout(promise, ms, fallback) {
  if (ms <= 0) return await promise;
  const timer = new Promise((r) => setTimeout(() => r(fallback), ms));
  return Promise.race([promise, timer]);
}

function env(name, fallback = "") {
  return process.env[name] || fallback;
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