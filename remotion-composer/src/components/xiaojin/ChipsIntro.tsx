/**
 * "Pattern 4" from video-studio's CLAUDE-xiaojin-editorial.md intro sequence
 * — no title card at all. Opens straight on the full-bleed speaker with
 * left-side label chips building one at a time. Sibling to IntroTitle.tsx,
 * but deliberately renders NO full-canvas background of its own (unlike the
 * other 3 intro variants) — the video underneath is the intro, this is just
 * a thin chip stack over it.
 *
 * Chip source: the same `chapters` list already threaded into
 * XiaojinEditorial for ChapterNav — reused here instead of asking the LLM
 * planner for a separate, redundant "intro chips" field. Caps at 4 chips
 * (source doc: "icon prefix + text, rounded dark pill", stagger 6 frames).
 */
import { interpolate, useCurrentFrame } from "remotion";
import { APPLE, ColorMode, PALETTES } from "./theme";

export interface ChipsIntroProps {
  /** Reuses ChapterNav's chapter list as the chip labels — no separate field needed. */
  chapters: { label: string; labelEn?: string }[];
  introOutFrame: number;
  colorMode?: ColorMode;
  labelFont?: string;
  /** Editor-only position override, read from the shared `intro` object's
   *  own x/y (meaningful for this variant only — see the schema's own
   *  description on intro.x/intro.y). Omit for the default top-left stack. */
  x?: number;
  y?: number;
}

const CHIP_STAGGER_FRAMES = 6;
const MAX_CHIPS = 4;

export const ChipsIntro: React.FC<ChipsIntroProps> = ({
  chapters,
  introOutFrame,
  colorMode = "warm",
  labelFont = "inherit",
  x = 96,
  y = 420,
}) => {
  const frame = useCurrentFrame();
  const palette = PALETTES[colorMode];
  if (frame > introOutFrame + 12) return null;

  const exit = interpolate(frame, [introOutFrame - 10, introOutFrame + 12], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: APPLE,
  });
  const groupOpacity = 1 - exit;

  const chips = chapters.slice(0, MAX_CHIPS);

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        display: "flex",
        flexDirection: "column",
        gap: 14,
        opacity: groupOpacity,
        pointerEvents: "none",
      }}
    >
      {chips.map((c, i) => {
        const start = 10 + i * CHIP_STAGGER_FRAMES;
        const chipIn = interpolate(frame, [start, start + 16], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: APPLE,
        });
        return (
          <div
            key={`${c.label}-${i}`}
            style={{
              opacity: chipIn,
              // Slide in from the left, per the source doc's Pattern 4 spec.
              transform: `translateX(${(1 - chipIn) * -48}px)`,
              display: "flex",
              alignItems: "center",
              gap: 10,
              background: "rgba(13,17,23,0.68)",
              backdropFilter: "blur(6px)",
              borderRadius: 999,
              padding: "10px 22px 10px 16px",
              width: "fit-content",
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: palette.accentAlt,
                flexShrink: 0,
              }}
            />
            <span
              style={{
                fontFamily: labelFont,
                fontSize: 22,
                fontWeight: 700,
                color: "#FFFFFF",
              }}
            >
              {c.labelEn || c.label}
            </span>
          </div>
        );
      })}
    </div>
  );
};
