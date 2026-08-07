/**
 * The one piece of hand-written, non-AI-authored logic mid-video razor cuts
 * for Arm B (AI-authored scenes) depends on — mounted BOTH by the live
 * in-browser preview (editor/components/Authored/AuthoredPreview.tsx) and
 * by the real server render (whatsapp_mvp/authored/authored_renderer.py's
 * generated Root.tsx), so there is exactly one implementation of the remap,
 * never two that could drift.
 *
 * WHY THIS EXISTS (see the plan's own Context section for the full
 * reasoning): Arm A can cut non-destructively by remapping a whole
 * structured props tree once, before rendering (mapPropsForCuts in
 * ../../cuts.ts) — every card component downstream just consumes
 * already-correct OUTPUT-frame numbers. Arm B has no such tree: every
 * AI-authored scene's mountFrame/endFrame/caption/b-roll timing is a
 * literal number baked directly into arbitrary generated code, driven by
 * ONE `const frame = useCurrentFrame();` declaration (confirmed: every
 * real generated scene on disk declares it exactly once). There is no
 * external data to rewrite — so instead this wrapper computes a
 * `sourceFrame` value and hands it to the AI's own component AS A PROP,
 * which the frozen prompt contract (scene_author.py) now requires the
 * AI to read instead of calling useCurrentFrame() itself for content
 * timing. In the zero-cuts case (the default for every job until a user
 * actually cuts something) `sourceFrame === outputFrame` exactly, by
 * outputToSource's own definition — so this is a byte-identical no-op
 * until cuts are actually used.
 *
 * The base video itself moves OUT of AI-authored code and into this
 * wrapper for a separate reason: OffthreadVideo doesn't consume
 * `sourceFrame` — it decodes whatever frame corresponds to its own mount
 * position in the Sequence tree. An AI-authored <OffthreadVideo src=.../>
 * with no trimBefore/trimAfter would keep playing the raw video
 * continuously in OUTPUT-frame space regardless of what sourceFrame says,
 * showing the wrong content across every cut boundary. There's no way to
 * make arbitrary per-job AI-generated video-rendering code cut-aware
 * without trusting/parsing that code, so CutVideo (already fully generic,
 * already proven in production for two different Arm A callers) renders it
 * here instead, once, correctly, always.
 */
import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { CutVideo } from "./CutVideo";
import { normalizeCuts, outputToSource, keptSegments, type VideoCut } from "../../cuts";

export interface AuthoredCutWrapperProps {
  videoSrc: string;
  videoVolume?: number;
  /** SOURCE-frame space, half-open removed ranges — same VideoCut shape Arm
   *  A already uses. Undefined/empty is the default (no cuts made yet). */
  cuts: VideoCut[] | undefined;
  /** The RAW, uncut source video's own full length in frames. */
  sourceDurationFrames: number;
  /** The AI-authored scene component itself — receives every one of
   *  `sceneProps` PLUS a computed `sourceFrame`, and must use the latter
   *  (not its own useCurrentFrame()) for all content-timing logic. */
  component: React.ComponentType<Record<string, unknown>>;
  /** Everything AuthoredScene's props contract already defines (broll,
   *  words, fps, durationInFrames, width, height, overrides, ...) — passed
   *  through unchanged; only `sourceFrame` is added on top here. */
  sceneProps: Record<string, unknown>;
}

export const AuthoredCutWrapper: React.FC<AuthoredCutWrapperProps> = ({
  videoSrc,
  videoVolume,
  cuts,
  sourceDurationFrames,
  component: AuthoredScene,
  sceneProps,
}) => {
  const outputFrame = useCurrentFrame();
  // Recomputed each frame, unmemoized — matches XiaojinEditorial.tsx's own
  // top-level cuts computation, which isn't memoized either; cuts arrays
  // are a handful of entries at most, the cost is negligible.
  const normalized = normalizeCuts(cuts, sourceDurationFrames);
  const sourceFrame = outputToSource(outputFrame, normalized);
  const segments = keptSegments(normalized, sourceDurationFrames);

  return (
    <AbsoluteFill style={{ backgroundColor: "#000000" }}>
      <CutVideo
        src={videoSrc}
        segments={segments}
        sourceDurationFrames={sourceDurationFrames}
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
        volume={videoVolume ?? 1}
      />
      <AuthoredScene {...sceneProps} sourceFrame={sourceFrame} />
    </AbsoluteFill>
  );
};
