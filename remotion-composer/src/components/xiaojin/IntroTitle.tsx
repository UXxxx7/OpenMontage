/**
 * Intro title scrim — a dark gradient overlay on top of the (near-full-screen,
 * per the intro SpeakerCard scene) speaker card, with an eyebrow label, a
 * large title, a divider, and a subtitle. This is "Pattern 2" from
 * video-studio's CLAUDE-xiaojin-editorial.md intro sequence (pure title
 * card / cinematic dark open) — the other 3 confirmed patterns (stats hook,
 * title+atmosphere, straight-in-with-chips) are not ported; they need their
 * own components if a job calls for them.
 *
 * Ported from `IntroTitle.tsx`, identical across all 4 video-studio
 * projects except for hardcoded text — generalized to take that text as
 * props instead.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { APPLE, ColorMode, H, PALETTES, W } from "./theme";

export interface IntroTitleProps {
  eyebrow: string;
  title: string;
  subtitle: string;
  /** Frame the intro ends and this scrim should fade out. */
  introOutFrame: number;
  colorMode?: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const IntroTitle: React.FC<IntroTitleProps> = ({
  eyebrow,
  title,
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
  const ty = (1 - enter) * 32;

  // Title gets its own spring-scale entrance (the "main headline entrance"
  // animation vocabulary) instead of just inheriting the scrim's fade+slide —
  // a plain fade is fine for the supporting eyebrow/subtitle text, but not
  // for the single biggest, most prominent element on screen.
  const titleScale = spring({ frame, fps, config: { damping: 14, stiffness: 220 } }) * 0.2 + 0.8;
  // Divider sweeps in from 0 width once the title has mostly landed, rather
  // than popping to full width with everything else.
  const dividerWidth = interpolate(frame, [14, 30], [0, 72], {
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
        background:
          "linear-gradient(180deg, rgba(13,17,23,0.88) 0%, rgba(13,17,23,0.55) 45%, rgba(13,17,23,0.78) 100%)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 24,
        opacity,
        transform: `translateY(${ty}px)`,
        pointerEvents: "none",
      }}
    >
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
          fontSize: 108,
          fontWeight: 800,
          color: "#FFFFFF",
          lineHeight: 1.0,
          textAlign: "center",
          padding: "0 56px",
          textShadow: "0 4px 32px rgba(0,0,0,0.40)",
          transform: `scale(${titleScale})`,
        }}
      >
        {title}
      </span>

      <div
        style={{
          width: dividerWidth,
          height: 4,
          borderRadius: 2,
          background: palette.accent,
          marginTop: 6,
        }}
      />

      <span
        style={{
          fontFamily: headingFont,
          fontSize: 34,
          fontWeight: 600,
          color: "rgba(255,255,255,0.72)",
          letterSpacing: 1,
        }}
      >
        {subtitle}
      </span>
    </div>
  );
};
