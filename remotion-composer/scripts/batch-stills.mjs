#!/usr/bin/env node
// Fast batch QA stills — bundles the Remotion project ONCE and launches Chrome ONCE,
// then renders every requested frame in-process, instead of one `npx remotion still`
// CLI invocation per frame (which pays a full Node+webpack+Chrome cold start every time).
//
// This file is meant to be copied into each motion/<project>/scripts/ folder (same
// convention as cut_source.py / build_data.py) so its bare `@remotion/*` imports
// resolve against that project's own node_modules via normal Node resolution — do not
// try to run it from tools/ directly against another project's node_modules.
//
// Usage (from inside a motion/<project> directory):
//   node scripts/batch-stills.mjs <compositionId> <outDir> <frame1,frame2,...> [scale] [propsFile] [bundleDir]
//
// Example:
//   node scripts/batch-stills.mjs RobertRaw out/qa 10,95,600,842,858 0.5
//   node scripts/batch-stills.mjs XiaojinEditorial out/qa 10,95,600 0.5 job_props.json ../build
//
// Fix C8b：可选的最后一个参数 bundleDir——whatsapp_mvp/remotion_bundle.py 已经
// 维护一份按 src mtime 失效的缓存 bundle（build/），单帧 `npx remotion still`
// 调用一直在复用它。传了 bundleDir 就直接当 serveUrl 用，跳过这里的
// bundle()——否则两边各打包一次，等于没省到 bundle 那部分的时间。不传时保留
// 原来的行为（自己 bundle 一次），其它调用方（例如独立跑这个脚本做一次性
// QA）不受影响。
// Fix C8（2026-07-16）：加了可选的 propsFile 参数——原始版本完全没传
// inputProps，composition 只能用 calculateMetadata 的默认值渲染。这对
// XiaojinEditorial 不成立：它的 calculateXiaojinEditorialMetadata 要靠
// props.durationSeconds 算 durationInFrames，props 本身还要过 ajv 校验，
// 不传 inputProps 直接渲染默认空 props 会失败或渲染出完全不对的东西。
// 每个具体任务（whatsapp_mvp/qa_stills.py）要看的是自己那份 job 的
// _op_apply_style_props.json，不是某个写死的示例——inputProps 要传给
// selectComposition 和 renderStill 两处（selectComposition 决定
// durationInFrames，renderStill 决定实际渲染内容），只传一处会导致
// duration 和内容对不上。
// Correctness note: this calls the exact same @remotion/renderer `renderStill()` that
// `npx remotion still` calls internally (the CLI is a thin wrapper around this same
// API) — same bundler, same Chrome, same frame-render code path.
//
// Verified 2026-07-06 on motion/robert-raw: benchmarked 14 real QA frames (the actual
// set used in that build's QA pass) against fresh `npx remotion still` CLI output for
// each. Wall time: 80s (CLI, sequential) -> 12s (this script) for the same 14 frames —
// setup (bundle+browser+selectComposition) is ~2s total instead of ~6.4s PER FRAME.
// Per-pixel comparison (ffmpeg ssim/blend=difference) showed SSIM 0.965-0.989 across
// all 14 frames, consistently — a visual diff of the biggest outlier (frame 600) showed
// only faint text/blur EDGE outlines, zero difference in layout, position, color, or
// content. This is normal Chrome/Skia sub-pixel anti-aliasing variance between separate
// browser launches (confirmed not a config mismatch: tried matching `gl` renderer
// default and `forceDeviceScaleFactor`, neither changed the SSIM at all), not a
// rendering bug — safe to use for QA purposes (layout/timing/overlap/fill checks are
// unaffected by sub-pixel noise). If a future check ever shows a LOW-SSIM outlier
// (e.g. <0.8) or a diff with solid blocks of difference (not just thin edges), that
// would indicate a real bug — re-verify against the CLI before trusting it.

import { bundle } from "@remotion/bundler";
import { renderStill, selectComposition, openBrowser } from "@remotion/renderer";
import path from "node:path";
import fs from "node:fs";

async function main() {
  const [, , compositionId, outDir, framesArg, scaleArg, propsFileArg, bundleDirArg] = process.argv;

  if (!compositionId || !outDir || !framesArg) {
    console.error(
      "Usage: node batch-stills.mjs <compositionId> <outDir> <frame1,frame2,...> [scale=0.5] [propsFile] [bundleDir]"
    );
    process.exit(1);
  }

  const frames = framesArg.split(",").map((f) => parseInt(f.trim(), 10));
  const scale = scaleArg ? parseFloat(scaleArg) : 0.5;
  const inputProps = propsFileArg
    ? JSON.parse(fs.readFileSync(propsFileArg, "utf-8"))
    : undefined;

  fs.mkdirSync(outDir, { recursive: true });

  const t0 = Date.now();
  let serveUrl;
  if (bundleDirArg) {
    serveUrl = path.resolve(bundleDirArg);
    console.log(`[batch-stills] reusing existing bundle at ${serveUrl}`);
  } else {
    // Ported from video-studio's tools/batch-stills — that project's entry is
    // src/index.ts; remotion-composer's is src/index.tsx.
    const entryPoint = path.resolve("src/index.tsx");
    console.log(`[batch-stills] bundling ${entryPoint} ...`);
    serveUrl = await bundle({ entryPoint, onProgress: () => {} });
  }
  const t1 = Date.now();
  console.log(`[batch-stills] bundle ready in ${((t1 - t0) / 1000).toFixed(1)}s`);

  console.log(`[batch-stills] launching Chrome (once) ...`);
  const browser = await openBrowser("chrome");
  const t2 = Date.now();
  console.log(`[batch-stills] browser ready in ${((t2 - t1) / 1000).toFixed(1)}s`);

  const composition = await selectComposition({
    serveUrl,
    id: compositionId,
    puppeteerInstance: browser,
    inputProps,
  });
  const t3 = Date.now();
  console.log(
    `[batch-stills] composition "${compositionId}" selected in ${((t3 - t2) / 1000).toFixed(1)}s`
  );

  const results = [];
  for (const frame of frames) {
    const output = path.join(outDir, `f${frame}.png`);
    const fStart = Date.now();
    await renderStill({
      composition,
      serveUrl,
      output,
      frame,
      puppeteerInstance: browser,
      scale,
      overwrite: true,
      inputProps,
    });
    const fDur = Date.now() - fStart;
    console.log(`[batch-stills] frame ${frame} -> ${output} (${fDur}ms)`);
    results.push({ frame, output, ms: fDur });
  }

  await browser.close({ silent: true });
  const total = Date.now() - t0;
  console.log(`[batch-stills] TOTAL: ${(total / 1000).toFixed(1)}s for ${frames.length} frames`);

  fs.writeFileSync(
    path.join(outDir, "batch-stills-manifest.json"),
    JSON.stringify({ compositionId, scale, totalMs: total, results }, null, 2)
  );
}

main().catch((err) => {
  console.error("[batch-stills] FAILED:", err);
  process.exit(1);
});
