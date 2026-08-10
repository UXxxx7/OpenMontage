/**
 * ProsConsCard — ComparisonCard's more opinionated sibling: two columns with
 * green checks / red X's baked into the visual, not neutral labeled columns.
 * Use when the moment is explicitly "why this choice matters" (a decision,
 * a warning, a before/after framing), not a neutral side-by-side attribute
 * comparison — that's what ComparisonCard is for.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface ProsConsCardProps {
  title?: string;
  prosLabel: string;
  consLabel: string;
  pros: string[];
  cons: string[];
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const ProsConsCard: React.FC<ProsConsCardProps> = ({
  title,
  prosLabel,
  consLabel,
  pros,
  cons,
  mountFrame,
  endFrame,
  x = 60,
  y = 900,
  width = 960,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
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
  const goodTint = `${palette.good}1F`;
  const badTint = `${palette.bad}1F`;

  const Column: React.FC<{ label: string; items: string[]; color: string; tint: string; mark: string; delayBase: number }> = ({
    label, items, color, tint, mark, delayBase,
  }) => (
    <div style={{ flex: 1, background: tint, borderRadius: 16, padding: "16px 16px 18px" }}>
      <div style={{
        fontFamily: headingFont, fontSize: 13, fontWeight: 800, letterSpacing: 1.5,
        textTransform: "uppercase", color, marginBottom: 12,
      }}>
        {label}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {items.map((item, i) => {
          const itemDelay = delayBase + i * 6;
          const itemLocal = local - itemDelay;
          const itemEntry = interpolate(itemLocal, [0, 10], [0, 1], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
          });
          return (
            <div
              key={i}
              style={{
                display: "flex", alignItems: "flex-start", gap: 8,
                opacity: itemEntry, transform: `translateY(${(1 - itemEntry) * 6}px)`,
              }}
            >
              <span style={{
                flexShrink: 0, width: 18, height: 18, borderRadius: "50%",
                background: color, color: "#FFFFFF",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 10, fontWeight: 900, marginTop: 1,
              }}>
                {mark}
              </span>
              <span style={{ fontFamily: labelFont, fontSize: 17, fontWeight: 600, color: palette.ink, lineHeight: 1.35 }}>
                {item}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );

  return (
    <div style={{ position: "absolute", left: x, top: y, width, opacity: cardEntry * exitFade }}>
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 18, fontWeight: 700, color: palette.ink,
          textAlign: "center", marginBottom: 16,
        }}>
          {title}
        </div>
      ) : null}
      <div style={{ display: "flex", gap: 16 }}>
        <Column label={prosLabel} items={pros.slice(0, 4)} color={palette.good} tint={goodTint} mark="✓" delayBase={6} />
        <Column label={consLabel} items={cons.slice(0, 4)} color={palette.bad} tint={badTint} mark="✕" delayBase={16} />
      </div>
    </div>
  );
};
