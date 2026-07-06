/**
 * Karaoke captions, post-xhs spec: 48px (bumped from 40px for platform
 * prominence), always-on dark pill, char-by-char terracotta sweep. Direct
 * phrase lookup only (not createTikTokStyleCaptions — merges CJK tokens).
 */
import { useCurrentFrame, useVideoConfig } from "remotion";
import { CAPTION_BOTTOM, COLOR, W } from "./theme";

export interface CaptionPhrase {
  text: string;
  startMs: number;
  endMs: number;
}

export interface CaptionsProps {
  captions: CaptionPhrase[];
  /** Suppress rendering before/after these frames (intro title / outro). */
  suppressBeforeFrame?: number;
  suppressAfterFrame?: number;
  font?: string;
  noSweepBelowMs?: number;
}

export const Captions: React.FC<CaptionsProps> = ({
  captions,
  suppressBeforeFrame = 0,
  suppressAfterFrame = Infinity,
  font = "inherit",
  noSweepBelowMs = 200,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (frame < suppressBeforeFrame || frame > suppressAfterFrame) return null;

  const ms = (frame / fps) * 1000;
  const active = captions.find((c) => ms >= c.startMs && ms < c.endMs);
  if (!active) return null;

  const duration = active.endMs - active.startMs;
  const progress =
    duration < noSweepBelowMs
      ? active.text.length
      : Math.floor(
          Math.max(0, Math.min(active.text.length, ((ms - active.startMs) / duration) * active.text.length))
        );

  return (
    <div
      style={{
        position: "absolute",
        left: 40,
        right: 40,
        bottom: CAPTION_BOTTOM,
        textAlign: "center",
      }}
    >
      <div
        style={{
          display: "inline-block",
          background: "rgba(42,38,32,0.82)",
          borderRadius: 12,
          padding: "10px 28px",
          maxWidth: W - 80,
        }}
      >
        {active.text.split("").map((ch, i) => (
          <span
            key={i}
            style={{
              fontFamily: font,
              fontSize: 48,
              fontWeight: 700,
              color: i < progress ? COLOR.terracotta : "#FFFFFF",
            }}
          >
            {ch}
          </span>
        ))}
      </div>
    </div>
  );
};
