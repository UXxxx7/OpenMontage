/**
 * RankedListCard — multiple numeric items compared at once ("top 3", "best
 * X"), each row a rank badge + label + horizontal bar that fills 0->value
 * with a count-up, staggered row by row. Fills the gap between count_up
 * (ONE number) and before_after (ONE metric, two points in time): this is
 * for several numbers shown side by side in the same beat.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface RankedListItem {
  label: string;
  labelEn?: string;
  value: number;
  /** Pre-formatted display value, e.g. "$8,400" or "94%". Falls back to Math.round(value). */
  displayValue?: string;
  suffix?: string;
}

export interface RankedListCardProps {
  title?: string;
  items: RankedListItem[];
  /** Bar-fill reference max. Defaults to the largest item's value. */
  maxValue?: number;
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
  /** Frames between each row's fill starting. Default 10. */
  rowStagger?: number;
}

export const RankedListCard: React.FC<RankedListCardProps> = ({
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
  rowStagger = 10,
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
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {items.map((item, i) => {
          const rowDelay = i * rowStagger;
          const rowLocal = Math.max(0, local - rowDelay);
          const rowEntry = interpolate(rowLocal, [0, 8], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          const fillPct = interpolate(rowLocal, [0, 40], [0, item.value / barMax], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
          });
          const shownValue = interpolate(rowLocal, [0, 40], [0, item.value], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
          });
          const isWinner = i === 0;
          const barColor = isWinner ? palette.accent : palette.inkSoft;
          return (
            <div key={i} style={{ opacity: rowEntry, transform: `translateX(${(1 - rowEntry) * -14}px)` }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <div style={{
                    width: 32, height: 32, borderRadius: "50%", flexShrink: 0,
                    background: isWinner ? palette.accent : "transparent",
                    border: isWinner ? "none" : `1.5px solid ${palette.inkSoft}`,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    fontFamily: headingFont, fontSize: 15, fontWeight: 800,
                    color: isWinner ? "#FFFFFF" : palette.inkSoft,
                  }}>
                    {i + 1}
                  </div>
                  <div style={{ display: "flex", flexDirection: "column" }}>
                    <span style={{ fontFamily: labelFont, fontSize: 20, fontWeight: 700, color: palette.ink }}>
                      {item.label}
                    </span>
                    {item.labelEn ? (
                      <span style={{
                        fontFamily: labelFont, fontSize: 11, fontWeight: 600, letterSpacing: 1.2,
                        color: palette.inkSoft, textTransform: "uppercase",
                      }}>
                        {item.labelEn}
                      </span>
                    ) : null}
                  </div>
                </div>
                <span style={{ fontFamily: headingFont, fontSize: 24, fontWeight: 800, color: palette.ink }}>
                  {item.displayValue ?? Math.round(shownValue)}{item.suffix ?? ""}
                </span>
              </div>
              <div style={{ height: 12, borderRadius: 999, background: palette.bgDeep, overflow: "hidden" }}>
                <div style={{
                  height: "100%", width: `${Math.max(0, Math.min(1, fillPct)) * 100}%`,
                  borderRadius: 999, background: barColor,
                }} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
