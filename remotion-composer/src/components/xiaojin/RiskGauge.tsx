/**
 * Semicircle risk/status gauge — "Risk consequence" row of compose-director.md's
 * Data Display Analysis table ("Arc gauge: COVERED → RISK zone animating").
 *
 * Ported from video-studio's motion/vell-renewal-fresh/src/RenewalFresh/LapseSection.tsx
 * (the `RiskGauge` inner function, lines 10-58) — generalized: end labels, card
 * title, and mount timing are now props instead of hardcoded insurance-specific
 * text and frame constants imported from a per-project generated/timeline.ts.
 *
 * Fixed while porting: the original's needle swings opposite to the direction
 * the arc fills (`angleRad = PI*(1-fillPct)` moves the needle toward the LEFT
 * end as fillPct/risk increases, while the colored arc visibly fills toward the
 * RIGHT/"at risk" end at the same time) — contradicts its own code comment
 * ("100% fill = pointing right") and reads as visually wrong once you look for
 * it. Here the needle tracks the same left→right sweep as the fill.
 *
 * The 3-zone hue plateau (green → flat amber band → red) is a deliberate design
 * choice from the original, not a bug — kept as-is.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface RiskGaugeProps {
  title: string;
  leftLabel: string;
  rightLabel: string;
  /** Final fill fraction, 0-1 (e.g. 1.0 = fully "at risk"). */
  value: number;
  /** Frame this card starts entering. */
  mountFrame: number;
  /** Frames after mount before the fill animation itself starts. Default 20. */
  fillDelayFrames?: number;
  /** How many frames the fill takes to animate from 0 to `value`. Default 50. */
  fillDurationFrames?: number;
  /** Frame this card starts fading out (15-frame fade). Omit to stay on screen for the rest of the video. */
  endFrame?: number;
  x?: number;
  y?: number;
  /** Fixed card width (e.g. the full 960px content-zone lane). Omit for content-sized. */
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

function gaugeColorFor(fillPct: number): string {
  if (fillPct < 0.4) return `hsl(${120 - fillPct * 2.5 * 120}, 80%, 42%)`;
  if (fillPct < 0.7) return `hsl(30, 90%, 48%)`;
  return `hsl(${5 + (1 - fillPct) * 20}, 85%, 45%)`;
}

const GaugeSvg: React.FC<{ fillPct: number; age: number; leftLabel: string; rightLabel: string }> = ({
  fillPct,
  age,
  leftLabel,
  rightLabel,
}) => {
  const R = 150;
  const cx = R + 16;
  const cy = R + 10;
  const SW = 20;
  const pathLen = Math.PI * R;

  const trackD = `M ${cx - R},${cy} A ${R},${R} 0 0,1 ${cx + R},${cy}`;
  const fillLen = fillPct * pathLen;

  // Needle tracks the same left(covered)->right(at risk) sweep the fill does,
  // matching the fill's own leading-edge position on the arc at t=fillPct.
  const angle = Math.PI * fillPct;
  const nx = cx - (R - 22) * Math.cos(angle);
  const ny = cy - (R - 22) * Math.sin(angle);

  const color = gaugeColorFor(fillPct);

  return (
    <div style={{ width: (R + 16) * 2, margin: "0 auto" }}>
      <svg width={(R + 16) * 2} height={R + 10} style={{ display: "block" }}>
        <path d={trackD} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth={SW} strokeLinecap="round" />
        <path
          d={trackD}
          fill="none"
          stroke={color}
          strokeWidth={SW}
          strokeLinecap="round"
          strokeDasharray={`${fillLen} ${pathLen}`}
          strokeDashoffset={0}
        />
        {age > 5 && (
          <>
            <line x1={cx} y1={cy} x2={nx} y2={ny} stroke={color} strokeWidth={5} strokeLinecap="round" />
            <circle cx={cx} cy={cy} r={9} fill={color} />
          </>
        )}
      </svg>
      {/* End labels as a flex row, NOT SVG text anchored to the arc's fixed
          edges — confirmed real regression from sizing this component up:
          at the bigger fontSize, long labels ("COVERAGE AT RISK" etc, up to
          16 chars per _plan_gauge's own truncation) collided in the middle
          of the now-only-modestly-wider arc. A flex row gives each label its
          own half of the width and truncates independently instead. */}
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginTop: 10, padding: "0 4px" }}>
        <span style={{
          fontSize: 16, fontWeight: 700, color: "rgba(79,157,105,0.8)",
          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        }}>
          {leftLabel}
        </span>
        <span style={{
          fontSize: 16, fontWeight: 700, color: "rgba(207,84,72,0.8)", textAlign: "right",
          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        }}>
          {rightLabel}
        </span>
      </div>
    </div>
  );
};

export const RiskGauge: React.FC<RiskGaugeProps> = ({
  title,
  leftLabel,
  rightLabel,
  value,
  mountFrame,
  fillDelayFrames = 20,
  fillDurationFrames = 50,
  endFrame,
  x = 80,
  y = 900,
  width,
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
  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 200 } });

  const fillAge = Math.max(0, local - fillDelayFrames);
  const fillPct = interpolate(fillAge, [0, fillDurationFrames], [0, value], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        opacity: cardEntry * exitFade,
        transform: `scale(${0.88 + 0.12 * cardEntry})`,
        transformOrigin: "top center",
        background: "rgba(13,17,23,0.88)",
        borderRadius: 24,
        padding: "28px 28px 20px",
        boxShadow: `0 8px 32px ${palette.shadow}`,
        // Fixed lane width centers the title/gauge (title is text-centered,
        // GaugeSvg uses margin auto) instead of hugging the left edge.
        width,
      }}
    >
      <div
        style={{
          fontFamily: headingFont,
          fontSize: 16,
          fontWeight: 600,
          color: "rgba(255,255,255,0.45)",
          letterSpacing: 3,
          textTransform: "uppercase",
          marginBottom: 20,
          textAlign: "center",
        }}
      >
        {title}
      </div>
      <GaugeSvg fillPct={fillPct} age={fillAge} leftLabel={leftLabel} rightLabel={rightLabel} />
    </div>
  );
};
