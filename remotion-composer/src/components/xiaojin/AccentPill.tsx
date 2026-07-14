/**
 * AccentPill — full-width terracotta takeaway pill, ported from the closing
 * move of video-studio's reference sections (vell-renewal-fresh's
 * CoverageSection "benefits pill": `background: COLOR.terracotta,
 * borderRadius: 50, padding: "30px 0"` with a single bold white line like
 * "Policy Active — Renew in 30 Days"). In the reference it's what completes a
 * content stack so the zone reads FULL rather than one lonely card floating
 * in cream space; here it renders the planner's per-data-point "pill" text
 * with the same springy back.out pop the project already uses for chips.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface AccentPillProps {
  text: string;
  mountFrame: number;
  /** Frame this pill starts fading out (15-frame fade). Omit to stay to the end. */
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
}

export const AccentPill: React.FC<AccentPillProps> = ({
  text,
  mountFrame,
  endFrame,
  x = 60,
  y = 1040,
  width = 960,
  colorMode,
  headingFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;
  if (endFrame !== undefined && frame >= endFrame) return null;
  const exitFade =
    endFrame !== undefined
      ? interpolate(frame, [endFrame - 15, endFrame], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
      : 1;

  const palette = PALETTES[colorMode];
  // Springy pop with visible overshoot — the project's chip/badge convention.
  const entry = spring({ frame: local, fps, config: { damping: 10, stiffness: 200 } });

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        background: palette.accent,
        borderRadius: 50,
        padding: "26px 32px",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        boxShadow: `0 8px 28px ${palette.shadow}`,
        opacity: entry * exitFade,
        transform: `translateY(${(1 - entry) * 18}px) scale(${0.85 + 0.15 * entry})`,
      }}
    >
      <span
        style={{
          fontFamily: headingFont,
          fontSize: 30,
          fontWeight: 800,
          color: "#FFFFFF",
          textAlign: "center",
          lineHeight: 1.2,
        }}
      >
        {text}
      </span>
    </div>
  );
};
