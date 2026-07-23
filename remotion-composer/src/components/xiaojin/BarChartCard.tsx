/**
 * BarChartCard — a real vertical column chart with value labels and a
 * baseline, for genuinely chart-shaped multi-value data. Deliberately
 * vertical (unlike RankedListCard's horizontal rows) so the two read as
 * different visual grammars: RankedList is a ranked LIST (rank badge +
 * label + bar), this is a CHART (no ranking implied, just magnitude
 * comparison across categories).
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface BarChartItem {
  label: string;
  value: number;
  /** Pre-formatted display value, e.g. "$40K". Falls back to Math.round(value). */
  displayValue?: string;
}

export interface BarChartCardProps {
  title?: string;
  items: BarChartItem[];
  /** Bar-height reference max. Defaults to the largest item's value. */
  maxValue?: number;
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
  /** Frames between each bar starting to grow. Default 8. */
  barStagger?: number;
}

const CHART_HEIGHT = 260;

export const BarChartCard: React.FC<BarChartCardProps> = ({
  title,
  items,
  maxValue,
  mountFrame,
  endFrame,
  x = 60,
  y = 900,
  width = 960,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
  barStagger = 8,
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
  const barMax = maxValue ?? Math.max(...items.map((it) => it.value), 1);
  const shown = items.slice(0, 4);
  const colGap = 24;
  const colWidth = (width - 56 * 2 - colGap * (shown.length - 1)) / shown.length;

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        background: palette.card, borderRadius: 20,
        padding: "26px 28px 22px", boxShadow: `0 8px 32px ${palette.shadow}`,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
      }}
    >
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginBottom: 20,
        }}>
          {title}
        </div>
      ) : null}
      <div style={{ display: "flex", alignItems: "flex-end", gap: colGap, height: CHART_HEIGHT }}>
        {shown.map((item, i) => {
          const barDelay = i * barStagger;
          const barLocal = Math.max(0, local - barDelay);
          const fillPct = interpolate(barLocal, [0, 36], [0, item.value / barMax], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
          });
          const shownValue = interpolate(barLocal, [0, 36], [0, item.value], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
          });
          const barHeight = Math.max(2, fillPct * CHART_HEIGHT);
          const labelFade = interpolate(barLocal, [0, 10], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          return (
            <div key={i} style={{ width: colWidth, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "flex-end", height: "100%" }}>
              <span style={{
                fontFamily: headingFont, fontSize: 18, fontWeight: 800, color: palette.ink,
                marginBottom: 8, opacity: labelFade,
              }}>
                {item.displayValue ?? Math.round(shownValue)}
              </span>
              <div style={{
                width: "100%", height: barHeight, borderRadius: "8px 8px 0 0",
                background: `linear-gradient(180deg, ${palette.accentAlt}, ${palette.accent})`,
              }} />
              <span style={{
                fontFamily: labelFont, fontSize: 12.5, fontWeight: 600, letterSpacing: 0.5,
                color: palette.inkSoft, textTransform: "uppercase", marginTop: 10, textAlign: "center",
              }}>
                {item.label}
              </span>
            </div>
          );
        })}
      </div>
      <div style={{ height: 2, background: palette.line, marginTop: 2 }} />
    </div>
  );
};
