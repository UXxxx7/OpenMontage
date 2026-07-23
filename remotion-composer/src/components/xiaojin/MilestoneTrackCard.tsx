/**
 * MilestoneTrackCard — a lightweight horizontal dot-track for 2-4 key dates
 * ("purchased -> claim filed -> renewal due"), lighting up one dot at a time.
 * Distinct from TimelineSection (the full-canvas dark takeover graphic):
 * this stays inside the normal content zone alongside other cards, for a
 * quick history/journey beat that doesn't warrant taking over the screen.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface MilestoneTrackItem {
  label: string;
  sublabel?: string;
}

export interface MilestoneTrackCardProps {
  title?: string;
  milestones: MilestoneTrackItem[];
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
  /** Frames between each dot lighting up. Default 18. */
  dotStagger?: number;
}

export const MilestoneTrackCard: React.FC<MilestoneTrackCardProps> = ({
  title,
  milestones,
  mountFrame,
  endFrame,
  x = 60,
  y = 900,
  width = 960,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
  dotStagger = 18,
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
  const count = milestones.length;
  const lastDotDelay = Math.max(0, (count - 1) * dotStagger);
  const trackFillPct = interpolate(local, [8, lastDotDelay + 14], [0, 100], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        background: palette.card, borderRadius: 20,
        padding: "28px 32px 24px", boxShadow: `0 8px 32px ${palette.shadow}`,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
      }}
    >
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginBottom: 30,
        }}>
          {title}
        </div>
      ) : null}
      <div style={{ position: "relative", height: 3, background: palette.line, margin: "0 8px" }}>
        <div style={{
          position: "absolute", top: 0, left: 0, height: "100%",
          width: `${trackFillPct}%`, background: palette.accent,
        }} />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: -8 }}>
        {milestones.slice(0, 4).map((m, i) => {
          const dotDelay = i * dotStagger;
          const dotPop = spring({ frame: Math.max(0, local - dotDelay), fps, config: { damping: 12, stiffness: 260 } });
          const lit = local >= dotDelay;
          return (
            <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: "center", width: `${100 / count}%` }}>
              <div style={{
                width: 20, height: 20, borderRadius: "50%",
                background: lit ? palette.accent : palette.bgDeep,
                boxShadow: `0 0 0 5px ${palette.card}`,
                transform: `scale(${lit ? 0.8 + 0.2 * dotPop : 1})`,
              }} />
              <div style={{
                fontFamily: labelFont, fontSize: 15, fontWeight: 700, color: palette.ink,
                marginTop: 12, textAlign: "center",
              }}>
                {m.label}
              </div>
              {m.sublabel ? (
                <div style={{
                  fontFamily: labelFont, fontSize: 12, fontWeight: 500, color: palette.inkSoft,
                  marginTop: 2, textAlign: "center",
                }}>
                  {m.sublabel}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
};
