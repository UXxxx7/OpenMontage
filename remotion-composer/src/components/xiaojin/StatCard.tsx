/**
 * StatCard -- white "paper" card for a single highlighted stat: a thin
 * red-to-yellow gradient strip along the top edge, a small bold red-orange
 * kicker, and one light-gray stat row (label left, big bold value + small
 * unit right). Ported from the OpenMontage-agentic-experiment sandbox build
 * (MrBeastAgentic/StatCard.tsx) that Marvelle reviewed and approved.
 *
 * NOT the shared InfoCard's dark-ink-panel look -- that panel color reads
 * fine against a light/warm canvas but nearly disappears against a
 * `colorMode="dark"` canvas (InfoCard's panel is a fixed near-black
 * rgba(13,17,23,0.86), and dark mode's own bg is #0D1117 -- almost the same
 * color). This card's white background stays legible on either canvas.
 * Single-row by design (unlike InfoCard, which supports multiple stacked
 * rows) -- use InfoCard when a moment needs more than one row grouped
 * together.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export interface StatCardProps {
  kicker: string;
  label: string;
  value: number;
  formatValue?: (v: number) => string;
  unit: string;
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  headingFont?: string;
  labelFont?: string;
}

export const StatCard: React.FC<StatCardProps> = ({
  kicker,
  label,
  value,
  formatValue,
  unit,
  mountFrame,
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

  const animatedValue = interpolate(local, [0, 40], [0, value], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const displayValue = formatValue ? formatValue(animatedValue) : `${Math.round(animatedValue)}`;

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
      <div style={{ padding: "26px 34px 30px" }}>
        <div
          style={{
            fontFamily: headingFont,
            fontSize: 20,
            fontWeight: 800,
            letterSpacing: 2,
            color: "#E0552F",
            marginBottom: 18,
          }}
        >
          {kicker.toUpperCase()}
        </div>
        <div
          style={{
            background: "#F3F1EC",
            borderRadius: 14,
            padding: "20px 26px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span style={{ fontFamily: labelFont, fontSize: 22, fontWeight: 700, letterSpacing: 1, color: "#5A5348" }}>
            {label.toUpperCase()}
          </span>
          <span>
            <span style={{ fontFamily: headingFont, fontSize: 44, fontWeight: 800, color: "#E0552F" }}>
              {displayValue}
            </span>
            <span style={{ fontFamily: labelFont, fontSize: 18, fontWeight: 700, color: "#8A8272", marginLeft: 8 }}>
              {unit.toUpperCase()}
            </span>
          </span>
        </div>
      </div>
    </div>
  );
};
