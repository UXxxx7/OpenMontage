/**
 * "Pattern 1" from video-studio's CLAUDE-xiaojin-editorial.md intro sequence
 * — dark full-bleed opener, a giant impact number/stat, a thin progress bar
 * beneath it, and up to two small supporting pill lines — then cuts to the
 * normal chrome once introOutFrame hits. Sibling to IntroTitle.tsx (Pattern
 * 2, "pure title card") — same props shape (eyebrow/title/subtitle/
 * introOutFrame) so the caller can swap variants without a different data
 * contract; this one just treats `title` as the hero stat text instead of a
 * headline, and renders `subtitle` as a second pill row alongside `eyebrow`
 * rather than a plain caption line.
 *
 * Deliberately does NOT include the "floating keyword constellation" some
 * other patterns in the source doc call for — kept out per explicit product
 * direction (no scattered background text, confirmed 2026-07-16).
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { APPLE, ColorMode, H, PALETTES, W } from "./theme";

export interface StatsHookIntroProps {
  /** Treated as the hero stat/number text (e.g. "100,000+"), not a headline. */
  title: string;
  eyebrow: string;
  subtitle: string;
  introOutFrame: number;
  colorMode?: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

const PILL_STAGGER_FRAMES = 6;

export const StatsHookIntro: React.FC<StatsHookIntroProps> = ({
  title,
  eyebrow,
  subtitle,
  introOutFrame,
  colorMode = "warm",
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = PALETTES[colorMode];
  if (frame > introOutFrame + 12) return null;

  const enter = interpolate(frame, [0, 22], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: APPLE,
  });
  const exit = interpolate(frame, [introOutFrame - 10, introOutFrame + 12], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: APPLE,
  });
  const opacity = enter * (1 - exit);

  const statScale = spring({ frame, fps, config: { damping: 14, stiffness: 220 } }) * 0.2 + 0.8;
  // Progress bar sweeps in under the stat once it's mostly landed — same
  // "sweep line" animation vocabulary entry IntroTitle's divider uses.
  const barWidth = interpolate(frame, [16, 40], [0, 420], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: APPLE,
  });

  const pills = [eyebrow, subtitle].filter(Boolean);

  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width: W,
        height: H,
        background: colorMode === "warm" ? "#0D1117" : palette.bgDeep,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 20,
        opacity,
        pointerEvents: "none",
      }}
    >
      <span
        style={{
          fontFamily: headingFont,
          fontSize: 128,
          fontWeight: 800,
          color: "#FFFFFF",
          lineHeight: 1.0,
          textAlign: "center",
          padding: "0 48px",
          textShadow: "0 4px 32px rgba(0,0,0,0.45)",
          transform: `scale(${statScale})`,
        }}
      >
        {title}
      </span>

      <div
        style={{
          width: barWidth,
          height: 6,
          borderRadius: 3,
          background: "rgba(255,255,255,0.12)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: "100%",
            height: "100%",
            background: `linear-gradient(90deg, ${palette.accentAlt}, ${palette.accent})`,
          }}
        />
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 14 }}>
        {pills.map((text, i) => {
          const pillStart = 26 + i * PILL_STAGGER_FRAMES;
          const pillIn = interpolate(frame, [pillStart, pillStart + 14], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: APPLE,
          });
          return (
            <div
              key={i}
              style={{
                opacity: pillIn,
                transform: `translateY(${(1 - pillIn) * 12}px)`,
                background: "rgba(255,255,255,0.08)",
                border: "1px solid rgba(255,255,255,0.14)",
                borderRadius: 999,
                padding: "10px 24px",
                fontFamily: labelFont,
                fontSize: 20,
                fontWeight: 600,
                letterSpacing: 0.5,
                color: "rgba(255,255,255,0.85)",
              }}
            >
              {text}
            </div>
          );
        })}
      </div>
    </div>
  );
};
