/**
 * ChecklistCard — items ticking on one by one, each on its own spoken beat.
 * Distinct emotional beat from StepList (which reads as "do this, then
 * this"): a checklist reads as "requirements met / features included" —
 * a clean green check-circle per item, matching the xiaojin-editorial
 * status-indicator convention (CLAUDE.md: "GOOD green circle prefix —
 * clean, no glow").
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface ChecklistItem {
  label: string;
  labelEn?: string;
  /** Frame (relative to the card's own mountFrame) this item ticks on. */
  activateOffset: number;
}

export interface ChecklistCardProps {
  title?: string;
  items: ChecklistItem[];
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

const CHECK_DRAW_FRAMES = 10;

export const ChecklistCard: React.FC<ChecklistCardProps> = ({
  title,
  items,
  mountFrame,
  endFrame,
  x = 60,
  y = 1040,
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

  return (
    <div style={{ position: "absolute", left: x, top: y, width, opacity: cardEntry * exitFade }}>
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginBottom: 14,
        }}>
          {title}
        </div>
      ) : null}
      {items.map((item, i) => {
        const itemLocal = local - item.activateOffset;
        const active = itemLocal >= 0;
        const pop = active
          ? spring({ frame: Math.min(itemLocal, 20), fps, config: { damping: 13, stiffness: 240 } })
          : 0;
        // Checkmark stroke draws in over CHECK_DRAW_FRAMES, starting a beat
        // after the box itself pops (reads as "box confirms, then check
        // draws" instead of both landing on the same frame).
        const drawProgress = interpolate(itemLocal - 4, [0, CHECK_DRAW_FRAMES], [0, 1], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp",
        });
        const dashLen = 20; // approx path length of the checkmark polyline below
        return (
          <div
            key={i}
            style={{
              display: "flex", alignItems: "center", gap: 18,
              background: active ? palette.card : "transparent",
              border: active ? "none" : `1.5px solid ${palette.line}`,
              borderRadius: 16,
              padding: "18px 22px",
              marginBottom: 12,
              opacity: active ? 1 : 0.88,
              boxShadow: active ? `0 8px 24px ${palette.shadow}` : "none",
              transform: `scale(${active ? 0.97 + 0.03 * pop : 1})`,
            }}
          >
            <div
              style={{
                width: 40, height: 40, borderRadius: 12, flexShrink: 0,
                background: active ? palette.good : "transparent",
                border: active ? "none" : `1.5px solid ${palette.inkSoft}`,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}
            >
              {active ? (
                <svg width={22} height={22} viewBox="0 0 22 22" fill="none">
                  <polyline
                    points="4,11 9,16 18,5"
                    stroke="#FFFFFF"
                    strokeWidth={3}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeDasharray={dashLen}
                    strokeDashoffset={dashLen * (1 - drawProgress)}
                  />
                </svg>
              ) : null}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span style={{
                fontFamily: labelFont, fontSize: 22, fontWeight: 700,
                color: active ? palette.ink : palette.inkSoft,
              }}>
                {item.label}
              </span>
              {item.labelEn ? (
                <span style={{
                  fontFamily: labelFont, fontSize: 12, fontWeight: 600, letterSpacing: 1.5,
                  color: palette.inkSoft, textTransform: "uppercase",
                }}>
                  {item.labelEn}
                </span>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
};
