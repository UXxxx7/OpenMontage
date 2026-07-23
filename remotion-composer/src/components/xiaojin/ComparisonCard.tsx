/**
 * ComparisonCard — side-by-side 2-3 column comparison ("Plan A vs Plan B",
 * "old way vs new way") across MULTIPLE attributes at once. Distinct from
 * before_after (InfoCard's beforeAfter mode), which only compares a SINGLE
 * metric across two points in time — this is for moments where several
 * differences are being listed side by side, not one number changing.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export type ComparisonAccent = "good" | "bad" | "neutral";

export interface ComparisonColumn {
  label: string;
  labelEn?: string;
  items: string[];
  accent?: ComparisonAccent;
}

export interface ComparisonCardProps {
  title?: string;
  columns: ComparisonColumn[];
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

function accentColor(accent: ComparisonAccent | undefined, palette: ReturnType<typeof paletteFor>) {
  if (accent === "good") return palette.good;
  if (accent === "bad") return palette.bad;
  return palette.accent;
}

function paletteFor(colorMode: ColorMode) {
  return PALETTES[colorMode];
}

export const ComparisonCard: React.FC<ComparisonCardProps> = ({
  title,
  columns,
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

  const palette = paletteFor(colorMode);
  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 200 } });
  const badgePop = spring({ frame: Math.max(0, local - 8), fps, config: { damping: 12, stiffness: 260 } });
  const colGap = 20;
  const colWidth = (width - colGap * (columns.length - 1)) / columns.length;

  return (
    <div style={{ position: "absolute", left: x, top: y, width, opacity: cardEntry * exitFade }}>
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginBottom: 14, textAlign: "center",
        }}>
          {title}
        </div>
      ) : null}
      <div style={{ position: "relative", display: "flex", gap: colGap }}>
        {columns.map((col, ci) => {
          const colDelay = ci * 6;
          const colEntry = spring({ frame: Math.max(0, local - colDelay), fps, config: { damping: 15, stiffness: 210 } });
          const dotColor = accentColor(col.accent, palette);
          return (
            <div
              key={ci}
              style={{
                width: colWidth,
                background: palette.card,
                borderRadius: 20,
                padding: "22px 22px 26px",
                boxShadow: `0 8px 32px ${palette.shadow}`,
                opacity: colEntry,
                transform: `translateY(${(1 - colEntry) * 20}px)`,
              }}
            >
              <div style={{
                display: "inline-flex", alignItems: "center", gap: 8,
                background: `${dotColor}1F`, borderRadius: 999, padding: "6px 14px", marginBottom: 16,
              }}>
                <span style={{ width: 8, height: 8, borderRadius: "50%", background: dotColor }} />
                <span style={{ fontFamily: headingFont, fontSize: 18, fontWeight: 800, color: palette.ink }}>
                  {col.label}
                </span>
              </div>
              {col.labelEn ? (
                <div style={{
                  fontFamily: labelFont, fontSize: 12, fontWeight: 600, letterSpacing: 1.5,
                  color: palette.inkSoft, textTransform: "uppercase", marginBottom: 14, marginTop: -10,
                }}>
                  {col.labelEn}
                </div>
              ) : null}
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {col.items.map((item, ii) => {
                  const itemDelay = colDelay + 10 + ii * 6;
                  const itemLocal = local - itemDelay;
                  const itemEntry = interpolate(itemLocal, [0, 10], [0, 1], {
                    extrapolateLeft: "clamp", extrapolateRight: "clamp",
                  });
                  return (
                    <div
                      key={ii}
                      style={{
                        display: "flex", alignItems: "flex-start", gap: 10,
                        opacity: itemEntry, transform: `translateX(${(1 - itemEntry) * -10}px)`,
                      }}
                    >
                      <span style={{
                        width: 6, height: 6, borderRadius: "50%", background: dotColor,
                        marginTop: 9, flexShrink: 0,
                      }} />
                      <span style={{ fontFamily: labelFont, fontSize: 19, fontWeight: 600, color: palette.ink, lineHeight: 1.35 }}>
                        {item}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
        {columns.length === 2 ? (
          <div
            style={{
              position: "absolute", left: "50%", top: "50%",
              transform: `translate(-50%, -50%) scale(${0.6 + 0.4 * badgePop})`,
              width: 52, height: 52, borderRadius: "50%",
              background: palette.ink, display: "flex", alignItems: "center", justifyContent: "center",
              boxShadow: `0 4px 16px ${palette.shadow}`,
              opacity: badgePop,
            }}
          >
            <span style={{ fontFamily: headingFont, fontSize: 16, fontWeight: 800, color: palette.bg }}>VS</span>
          </div>
        ) : null}
      </div>
    </div>
  );
};
