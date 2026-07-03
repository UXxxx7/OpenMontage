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

// ── BullMQ worker ────────────────────────────────────────────────────────────
const worker = new Worker(
  "video-jobs",
  async (job) => {
    const { waNumber, prompt } = job.data;
    const jobId = job.id;

    console.log(`[worker] starting job ${jobId} for ${waNumber}`);

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
        if (code === 0) resolve();
        else reject(new Error(`agent exited with code ${code}`));
      });
    });
  },
  {
    connection: redis,
    concurrency: 2,
    limiter: { max: 4, duration: 60_000 },
  }
);

worker.on("failed", (job, err) => {
  console.error(`[worker] job ${job?.id} failed:`, err.message);
  // Error message is sent to WhatsApp by runner.py directly
});

console.log("[worker] listening for video-jobs…");
