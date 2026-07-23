/**
 * ProgressBarCard — a straight linear completion bar ("4 of 5 steps done").
 * Distinct from RiskGauge (a semicircle framed as safe-vs-at-risk) and
 * CountdownRing (a circular days-remaining reveal): this is for a plain
 * "how far along is this" moment with no risk/time framing at all.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface ProgressBarCardProps {
  title?: string;
  label: string;
  percent: number; // 0-100
  subtext?: string;
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
  /** Frames for the fill to travel from 0 to its final percent. Default 40. */
  fillFrames?: number;
}

export const ProgressBarCard: React.FC<ProgressBarCardProps> = ({
  title,
  label,
  percent,
  subtext,
  mountFrame,
  endFrame,
  x = 60,
  y = 1040,
  width = 960,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
  fillFrames = 40,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;
  if (endFrame !== undefined && frame >= endFrame) return null;
  const exitFade = endFrame !== undefined
    ? interpolate(frame, [endFrame - 15, endFrame], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  const palette = PALETTES[colorMode];
  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 200 } });
  const clamped = Math.max(0, Math.min(100, percent));
  const fillPct = interpolate(local, [0, fillFrames], [0, clamped], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  const shownPct = Math.round(fillPct);

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        background: palette.card, borderRadius: 20,
        padding: "26px 28px", boxShadow: `0 8px 32px ${palette.shadow}`,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
      }}
    >
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginBottom: 18,
        }}>
          {title}
        </div>
      ) : null}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 12 }}>
        <span style={{ fontFamily: labelFont, fontSize: 22, fontWeight: 600, color: palette.ink }}>
          {label}
        </span>
        <span style={{ fontFamily: headingFont, fontSize: 30, fontWeight: 800, color: palette.accent }}>
          {shownPct}%
        </span>
      </div>
      <div style={{ height: 18, borderRadius: 999, background: palette.bgDeep, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${fillPct}%`, borderRadius: 999,
          background: `linear-gradient(90deg, ${palette.accent}, ${palette.accentAlt})`,
        }} />
      </div>
      {subtext ? (
        <div style={{ marginTop: 12, fontFamily: labelFont, fontSize: 15, fontWeight: 500, color: palette.inkSoft }}>
          {subtext}
        </div>
      ) : null}
    </div>
  );
};
