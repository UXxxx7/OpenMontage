/**
 * "Pattern 3" from video-studio's CLAUDE-xiaojin-editorial.md intro sequence
 * — an opaque full-bleed title card (not a translucent scrim over the video
 * like IntroTitle/Pattern 2) with the single largest piece of type in the
 * whole build, plus a small top-right brand chip. Sibling to IntroTitle.tsx,
 * same props shape.
 *
 * The source doc also calls for a "floating keyword constellation" behind
 * the title. Deliberately NOT included here — dropped per explicit product
 * direction (no scattered/floating background text, confirmed 2026-07-16) —
 * everything else about the pattern (opaque card, oversized title, brand
 * chip, then transition to the normal chrome) is intact.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { APPLE, ColorMode, PALETTES, W, H } from "./theme";

export interface TitleImpactIntroProps {
  eyebrow: string;
  title: string;
  subtitle: string;
  introOutFrame: number;
  /** Small top-right chip, e.g. the channel/brand name. Omit to skip the chip. */
  brandLabel?: string;
  colorMode?: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const TitleImpactIntro: React.FC<TitleImpactIntroProps> = ({
  eyebrow,
  title,
  subtitle,
  introOutFrame,
  brandLabel,
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

  // The single biggest moment in the whole build (per the source doc) — a
  // heavier spring than IntroTitle's headline, more overshoot-adjacent feel
  // while still landing clean (no bounce past 1.0, per the APPLE-only "no
  // overshoot" convention the rest of this style codex uses for card travel).
  const titleScale = spring({ frame, fps, config: { damping: 13, stiffness: 200 } }) * 0.25 + 0.75;
  const chipIn = interpolate(frame, [30, 46], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: APPLE,
  });

  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width: W,
        height: H,
        background: colorMode === "warm" ? palette.bg : palette.bgDeep,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 22,
        opacity,
        pointerEvents: "none",
      }}
    >
      {brandLabel ? (
        <div
          style={{
            position: "absolute",
            top: 64,
            right: 56,
            opacity: chipIn,
            transform: `translateY(${(1 - chipIn) * -10}px)`,
            background: colorMode === "warm" ? "rgba(255,255,255,0.75)" : "rgba(255,255,255,0.08)",
            border: `1px solid ${palette.line}`,
            borderRadius: 999,
            padding: "8px 18px",
            fontFamily: labelFont,
            fontSize: 14,
            fontWeight: 700,
            letterSpacing: 2,
            color: palette.accent,
          }}
        >
          {brandLabel.toUpperCase()}
        </div>
      ) : null}

      <span
        style={{
          fontFamily: labelFont,
          fontSize: 18,
          letterSpacing: 7,
          color: palette.accent,
          fontWeight: 700,
        }}
      >
        {eyebrow}
      </span>

      <span
        style={{
          fontFamily: headingFont,
          fontSize: 132,
          fontWeight: 800,
          color: palette.ink,
          lineHeight: 0.98,
          textAlign: "center",
          padding: "0 40px",
          transform: `scale(${titleScale})`,
        }}
      >
        {title}
      </span>

      <span
        style={{
          fontFamily: headingFont,
          fontSize: 30,
          fontWeight: 600,
          color: palette.inkSoft,
          letterSpacing: 0.5,
        }}
      >
        {subtitle}
      </span>
    </div>
  );
};
