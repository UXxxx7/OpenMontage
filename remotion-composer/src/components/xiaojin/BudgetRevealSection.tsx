/**
 * BudgetRevealSection -- bespoke before/after comparison for a dramatic
 * two-value reveal (e.g. a cost/metric that jumped or dropped over time).
 * Ported from the OpenMontage-agentic-experiment sandbox build
 * (MrBeastAgentic/BudgetRevealSection.tsx, originally "the budget:
 * $100K -> $1.5M") that Marvelle reviewed and approved, generalized here so
 * any before/after pair can use it: ONE unified white card, gradient top
 * strip (matching StatCard), two stat columns side by side with a small
 * trend-arrow icon between them once the second value has landed.
 *
 * Unlike the sandbox original, this version takes an `endFrame` and fades
 * out (the sandbox video ended right after this card mounted, so it never
 * needed to leave; a reusable component embedded in an arbitrary-length
 * video must not stay mounted forever once another beat needs the slot --
 * see InfoCard's doc comment for the confirmed bug this exact omission
 * caused elsewhere).
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export interface BudgetRevealSectionProps {
  kicker: string;
  leftLabel: string;
  leftValue: number;
  leftPrefix?: string;
  leftSuffix?: string;
  leftDecimals?: number;
  rightLabel: string;
  rightValue: number;
  rightPrefix?: string;
  rightSuffix?: string;
  rightDecimals?: number;
  mountFrame: number;
  secondRevealFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  headingFont?: string;
  labelFont?: string;
}

const fmt = (v: number, decimals = 0) => (decimals > 0 ? v.toFixed(decimals) : Math.round(v).toLocaleString());

export const BudgetRevealSection: React.FC<BudgetRevealSectionProps> = ({
  kicker,
  leftLabel,
  leftValue,
  leftPrefix = "",
  leftSuffix = "",
  leftDecimals = 0,
  rightLabel,
  rightValue,
  rightPrefix = "",
  rightSuffix = "",
  rightDecimals = 0,
  mountFrame,
  secondRevealFrame,
  endFrame,
  x = 80,
  y = 900,
  width = 920,
  headingFont = "inherit",
  labelFont = "inherit",
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
  const cardEntry = spring({ frame: local, fps, config: { damping: 16, stiffness: 220 } });

  const leftAnimated = interpolate(frame, [mountFrame, mountFrame + 40], [0, leftValue], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const rightAnimated = interpolate(frame, [secondRevealFrame, secondRevealFrame + 40], [0, rightValue], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const arrowShown = frame >= secondRevealFrame;
  const arrowScale = arrowShown
    ? spring({ frame: frame - secondRevealFrame, fps, config: { damping: 14, stiffness: 220 } })
    : 0;

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 24}px)`,
        background: "#FFFFFF",
        borderRadius: 24,
        boxShadow: "0 20px 50px rgba(0,0,0,0.35)",
        overflow: "hidden",
      }}
    >
      <div style={{ height: 6, width: "100%", background: "linear-gradient(90deg,#E0552F,#E8A13C)" }} />
      <div style={{ padding: "26px 34px 34px" }}>
        <div
          style={{
            fontFamily: headingFont,
            fontSize: 20,
            fontWeight: 800,
            letterSpacing: 2,
            color: "#E0552F",
            marginBottom: 22,
          }}
        >
          {kicker.toUpperCase()}
        </div>

        <div style={{ display: "flex", alignItems: "center" }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontFamily: labelFont, fontSize: 16, fontWeight: 700, letterSpacing: 1, color: "#8A8272", marginBottom: 6 }}>
              {leftLabel.toUpperCase()}
            </div>
            <div style={{ fontFamily: headingFont, fontSize: 52, fontWeight: 800, color: "#2A2620", lineHeight: 1 }}>
              {leftPrefix}{fmt(leftAnimated, leftDecimals)}{leftSuffix}
            </div>
          </div>

          <div style={{ width: 70, textAlign: "center", transform: `scale(${arrowScale})`, opacity: arrowShown ? 1 : 0 }}>
            <span style={{ fontSize: 34, color: "#E0552F" }}>{"↗"}</span>
          </div>

          <div style={{ flex: 1, textAlign: "right" }}>
            <div style={{ fontFamily: labelFont, fontSize: 16, fontWeight: 700, letterSpacing: 1, color: "#E0552F", marginBottom: 6 }}>
              {rightLabel.toUpperCase()}
            </div>
            <div style={{ fontFamily: headingFont, fontSize: 52, fontWeight: 800, color: "#E0552F", lineHeight: 1 }}>
              {rightPrefix}{fmt(rightAnimated, rightDecimals)}{rightSuffix}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
