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
  /** Editor-set override for where the caption box sits (props.captionPosition[0]
   *  — a single-entry array so the editor's existing per-item x/y/width drag
   *  overlay works on it unmodified, see positioning.ts/geometry.ts). Absent
   *  (every job the editor hasn't touched) keeps the original centered-bottom
   *  layout below, byte-identical to before this prop existed. */
  position?: { x: number; y: number; width?: number };
}

export const Captions: React.FC<CaptionsProps> = ({
  captions,
  introOutFrame = 0,
  colorMode = "warm",
  font = "inherit",
  fontSize = 42,
  maxWidth = 960,
  noSweepBelowMs = 200,
  position,
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

  // Fix C36 (2026-07-21, caught by vision QA on a real render): the raw
  // `progress` above is a continuous character count driven purely by
  // elapsed time within the phrase -- it has no notion of word boundaries,
  // so on nearly every phrase's sweep there's a frame where it lands
  // strictly inside a word ("spending" rendered as an orange "s" + white
  // "pending"). Not rare -- it's just been slipping past QA's sparse frame
  // sampling by chance until now. Snap back to the end of the last word
  // `progress` has already fully covered; a word flips color all at once,
  // never mid-character.
  let snappedProgress = progress;
  if (
    progress > 0 &&
    progress < active.text.length &&
    active.text[progress] !== " " &&
    active.text[progress - 1] !== " "
  ) {
    const lastSpace = active.text.lastIndexOf(" ", progress - 1);
    snappedProgress = lastSpace === -1 ? 0 : lastSpace + 1;
  }

  const chars = active.text.split("");
  const boxWidth = position?.width ?? maxWidth;

  return (
    <div
      style={{
        position: "absolute",
        left: position ? position.x : (W - maxWidth) / 2,
        top: position ? position.y : undefined,
        bottom: position ? undefined : CAPTION_BOTTOM,
        width: boxWidth,
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
          maxWidth: boxWidth,
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
              color: i < snappedProgress ? palette.captionHighlight : palette.captionText,
            }}
          >
            {ch}
          </span>
        ))}
      </div>
    </div>
  );
};
