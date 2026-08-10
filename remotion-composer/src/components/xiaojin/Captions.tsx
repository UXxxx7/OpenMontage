/**
 * Kinetic captions with character-by-character karaoke color reveal.
 *
 * Deliberately does NOT use @remotion/captions' createTikTokStyleCaptions —
 * video-studio's CLAUDE-v2.md documents (§5b) that it merges all tokens into
 * one page for Cantonese/Mandarin, breaking sync. This uses the direct
 * phrase lookup they moved to instead:
 *   captions.find((c) => ms >= c.startMs && ms < c.endMs)
 *
 * Progress within a phrase is `interpolate(ms, [startMs, endMs], [0, chars.length])`,
 * floored to a highlighted character count — same formula as their
 * `Captions.tsx` v4 reference implementation.
 */
import { useCurrentFrame, useVideoConfig } from "remotion";
import { CAPTION_BOTTOM, ColorMode, PALETTES, W } from "./theme";

export interface CaptionPhrase {
  text: string;
  startMs: number;
  endMs: number;
}

export interface CaptionsProps {
  captions: CaptionPhrase[];
  /** Frame before which no captions render (e.g. during a speakerless intro). */
  introOutFrame?: number;
  colorMode?: ColorMode;
  font?: string;
  fontSize?: number;
  maxWidth?: number;
  /** Phrases shorter than this are fully highlighted immediately, no sweep. */
  noSweepBelowMs?: number;
}

export const Captions: React.FC<CaptionsProps> = ({
  captions,
  introOutFrame = 0,
  colorMode = "warm",
  font = "inherit",
  fontSize = 42,
  maxWidth = 960,
  noSweepBelowMs = 200,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = PALETTES[colorMode];

  if (frame < introOutFrame) return null;

  const ms = (frame / fps) * 1000;
  const active = captions.find((c) => ms >= c.startMs && ms < c.endMs);
  if (!active) return null;

  const duration = active.endMs - active.startMs;
  const progress =
    duration < noSweepBelowMs
      ? active.text.length
      : Math.floor(
          Math.max(
            0,
            Math.min(
              active.text.length,
              ((ms - active.startMs) / duration) * active.text.length
            )
          )
        );

  const chars = active.text.split("");

  return (
    <div
      style={{
        position: "absolute",
        left: (W - maxWidth) / 2,
        bottom: CAPTION_BOTTOM,
        width: maxWidth,
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          background: palette.captionBg,
          borderRadius: palette.captionBg === "transparent" ? 0 : 14,
          padding: palette.captionBg === "transparent" ? 0 : "14px 28px",
          maxWidth,
          textAlign: "center",
          lineHeight: 1.3,
        }}
      >
        {chars.map((ch, i) => (
          <span
            key={i}
            style={{
              fontFamily: font,
              fontSize,
              fontWeight: 700,
              color: i < progress ? palette.captionHighlight : palette.captionText,
            }}
          >
            {ch}
          </span>
        ))}
      </div>
    </div>
  );
};
