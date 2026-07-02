import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
config({ path: resolve(dirname(fileURLToPath(import.meta.url)), "../.env") });
import express from "express";
import crypto from "crypto";
import { Queue } from "bullmq";
import IORedis from "ioredis";

const app = express();
const redis = new IORedis(process.env.REDIS_URL || "redis://localhost:6379", {
  maxRetriesPerRequest: null,
});
const videoQueue = new Queue("video-jobs", { connection: redis });

app.use(express.json());

// ── Privacy policy page (required for Meta app publishing) ──────────
app.get("/privacy", (req, res) => {
  res.send(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>Privacy Policy</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;padding:0 20px">
<h1>Privacy Policy</h1>
<p>This application (wa-montage) is a WhatsApp chatbot for automated video production.</p>
<h2>Data We Collect</h2>
<p>We receive WhatsApp messages you send to our service number. Message content is used solely to generate video content as requested.</p>
<h2>Data Retention</h2>
<p>Messages and generated videos are stored temporarily and deleted after delivery.</p>
<h2>Contact</h2>
<p>Questions: yuboliu030@gmail.com</p>
</body></html>`);
});

// ── Webhook verification (Meta requires GET on first setup) ──────────
app.get("/webhook", (req, res) => {
  const mode = req.query["hub.mode"];
  const token = req.query["hub.verify_token"];
  const challenge = req.query["hub.challenge"];
  if (mode === "subscribe" && token === process.env.WA_VERIFY_TOKEN) {
    console.log("Webhook verified");
    return res.send(challenge);
  }
  res.sendStatus(403);
});

// ── Incoming WhatsApp message ────────────────────────────────────────
app.post("/webhook", (req, res) => {
  // Validate Meta signature
  const sig = req.headers["x-hub-signature-256"] || "";
  const expected = "sha256=" + crypto
    .createHmac("sha256", process.env.WA_APP_SECRET)
    .update(JSON.stringify(req.body))
    .digest("hex");
  if (sig !== expected) return res.sendStatus(401);

  const entry = req.body?.entry?.[0];
  const change = entry?.changes?.[0];
  const message = change?.value?.messages?.[0];

  if (!message) return res.sendStatus(200); // not a message event

  const waNumber = message.from;           // sender's WhatsApp number
  const msgType = message.type;
  const text = message.text?.body?.trim();
  const msgId = message.id;

  console.log(`[webhook] from=${waNumber} type=${msgType} text="${text}"`);

  // ── Approval reply: user responds to an awaiting job ────────────────
  if (text && /^[1-9]$/.test(text)) {
    redis.publish(`approval:${waNumber}`, text);
    return res.sendStatus(200);
  }

  // ── New video request ────────────────────────────────────────────────
  if (msgType === "text" && text) {
    videoQueue.add("generate", {
      waNumber,
      prompt: text,
      msgId,
    }, {
      attempts: 2,
      backoff: { type: "exponential", delay: 5000 },
    });
    console.log(`[queue] job enqueued for ${waNumber}`);
  }

  res.sendStatus(200);
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`WA webhook listening on :${PORT}`));
