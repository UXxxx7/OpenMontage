import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
config({ path: resolve(dirname(fileURLToPath(import.meta.url)), "../.env") });
import { Worker } from "bullmq";
import IORedis from "ioredis";
import { spawn } from "child_process";
import path from "path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AGENT_DIR = path.resolve(__dirname, "../agent");

const redis = new IORedis(process.env.REDIS_URL || "redis://localhost:6379", {
  maxRetriesPerRequest: null,
});

// ── WhatsApp send helpers (imported inline to avoid circular deps) ───
async function sendText(to, text) {
  const { default: axios } = await import("axios");
  await axios.post(
    `https://graph.facebook.com/v19.0/${process.env.WA_PHONE_ID}/messages`,
    {
      messaging_product: "whatsapp",
      to,
      type: "text",
      text: { body: text },
    },
    { headers: { Authorization: `Bearer ${process.env.WA_TOKEN}` } }
  );
}

async function sendVideo(to, videoPath, caption = "") {
  const { default: axios } = await import("axios");
  const { default: FormData } = await import("form-data");
  const { createReadStream } = await import("fs");

  // 1. Upload media
  const form = new FormData();
  form.append("file", createReadStream(videoPath), { contentType: "video/mp4" });
  form.append("type", "video/mp4");
  form.append("messaging_product", "whatsapp");

  const upload = await axios.post(
    `https://graph.facebook.com/v19.0/${process.env.WA_PHONE_ID}/media`,
    form,
    {
      headers: {
        ...form.getHeaders(),
        Authorization: `Bearer ${process.env.WA_TOKEN}`,
      },
    }
  );
  const mediaId = upload.data.id;

  // 2. Send video message
  await axios.post(
    `https://graph.facebook.com/v19.0/${process.env.WA_PHONE_ID}/messages`,
    {
      messaging_product: "whatsapp",
      to,
      type: "video",
      video: { id: mediaId, caption },
    },
    { headers: { Authorization: `Bearer ${process.env.WA_TOKEN}` } }
  );
}

// ── Progress relay: reads Redis channel published by Python agent ────
function watchProgress(waNumber, jobId, abortSignal) {
  const sub = new IORedis(process.env.REDIS_URL || "redis://localhost:6379");
  const channel = `progress:${jobId}`;

  sub.subscribe(channel, (err) => {
    if (err) console.error("[progress] subscribe error", err);
  });

  sub.on("message", async (_ch, raw) => {
    try {
      const msg = JSON.parse(raw);

      if (msg.type === "text") {
        await sendText(waNumber, msg.body);
      } else if (msg.type === "video") {
        await sendVideo(waNumber, msg.path, msg.caption || "");
        sub.quit();
      } else if (msg.type === "approval") {
        // send options to user, agent is already waiting on Redis
        await sendText(waNumber, msg.body);
      }
    } catch (e) {
      console.error("[progress] relay error", e);
    }
  });

  abortSignal.addEventListener("abort", () => sub.quit());
}

// ── BullMQ worker ────────────────────────────────────────────────────
const worker = new Worker(
  "video-jobs",
  async (job) => {
    const { waNumber, prompt } = job.data;
    const jobId = job.id;

    console.log(`[worker] starting job ${jobId} for ${waNumber}`);
    await sendText(waNumber, "收到！正在為你製作視頻，請稍候 ⏳");

    const abort = new AbortController();
    watchProgress(waNumber, jobId, abort.signal);

    await new Promise((resolve, reject) => {
      const proc = spawn(
        "python3",
        ["-m", "runner", "--job-id", jobId, "--prompt", prompt, "--wa-number", waNumber],
        {
          cwd: AGENT_DIR,
          env: { ...process.env, PYTHONPATH: AGENT_DIR },
          stdio: ["ignore", "inherit", "inherit"],
        }
      );

      proc.on("close", (code) => {
        abort.abort();
        if (code === 0) resolve();
        else reject(new Error(`agent exited with code ${code}`));
      });
    });
  },
  {
    connection: redis,
    concurrency: 2,            // process 2 jobs at a time
    limiter: { max: 4, duration: 60_000 }, // max 4 jobs/min
  }
);

worker.on("failed", async (job, err) => {
  console.error(`[worker] job ${job?.id} failed:`, err.message);
  try {
    await sendText(
      job.data.waNumber,
      "抱歉，視頻製作失敗了 😢 請稍後再試，或換個主題描述。"
    );
  } catch {}
});

console.log("[worker] listening for video-jobs…");
