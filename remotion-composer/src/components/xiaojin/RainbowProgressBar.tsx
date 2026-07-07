/**
 * Thin rainbow bar pinned to the bottom edge, fills left-to-right over the
 * video's duration. Ported as-is from video-studio's ProgressBar.tsx — this
 * one was already fully generic (no per-project data), just renamed to
 * avoid colliding with the existing generic `components/ProgressBar.tsx`
 * (which is a data-viz stat bar, a different thing).
 */
import { useCurrentFrame, useVideoConfig } from "remotion";
import { PROGRESS, RAINBOW, W } from "./theme";

export const RainbowProgressBar: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const pct = Math.min(1, frame / (durationInFrames - 1));
  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        top: PROGRESS.y,
        width: W,
        height: PROGRESS.h,
        background: "rgba(0,0,0,0.10)",
      }}
    >
      <div style={{ width: W * pct, height: "100%", background: RAINBOW }} />
    </div>
  );
};
