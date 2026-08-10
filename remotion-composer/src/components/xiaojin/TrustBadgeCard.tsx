/**
 * TrustBadgeCard — icon + credential/stat rows, stacked ("Licensed Agent —
 * CA License #88291", "8 Years — Serving Bay Area families"). Nothing else
 * in the library builds authority/credibility specifically — most of these
 * videos are a person selling trust, and this is the visual for that beat.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export type TrustIconName = "shield" | "star" | "award" | "clock";

const ICON_PATHS: Record<TrustIconName, React.ReactNode> = {
  shield: <path d="M12 2 4 5v6c0 5 3.4 8.6 8 9.9 4.6-1.3 8-4.9 8-9.9V5l-8-3Zm0 2.2 6 2.25v4.55c0 4-2.6 6.9-6 7.86-3.4-.96-6-3.86-6-7.86V6.45l6-2.25Z" />,
  star: <path d="M12 2.5 14.9 9l7 .8-5.2 4.7 1.5 6.8-6.2-3.6-6.2 3.6 1.5-6.8-5.2-4.7 7-.8 3.1-6.5Z" />,
  award: (
    <>
      <circle cx="12" cy="8.5" r="6" />
      <path d="M8.5 13.8 7 22l5-2.8 5 2.8-1.5-8.2" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.5 2" />
    </>
  ),
};

const TrustIcon: React.FC<{ name: TrustIconName; color: string }> = ({ name, color }) => (
  <svg width={20} height={20} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    {ICON_PATHS[name]}
  </svg>
);

export interface TrustBadgeItem {
  icon: TrustIconName;
  primary: string;
  secondary: string;
}

export interface TrustBadgeCardProps {
  title?: string;
  badges: TrustBadgeItem[];
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
  /** Frames between each row's entrance. Default 18. */
  rowStagger?: number;
}

export const TrustBadgeCard: React.FC<TrustBadgeCardProps> = ({
  title,
  badges,
  mountFrame,
  endFrame,
  x = 60,
  y = 900,
  width = 960,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
  rowStagger = 18,
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
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {badges.slice(0, 3).map((b, i) => {
          const rowDelay = i * rowStagger;
          const rowLocal = local - rowDelay;
          const rowEntry = interpolate(rowLocal, [0, 10], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          const iconPop = spring({ frame: Math.max(0, rowLocal - 4), fps, config: { damping: 12, stiffness: 260 } });
          return (
            <div
              key={i}
              style={{
                display: "flex", alignItems: "center", gap: 14,
                background: palette.card, borderRadius: 16,
                padding: "16px 20px", boxShadow: `0 6px 20px ${palette.shadow}`,
                opacity: rowEntry, transform: `translateX(${(1 - rowEntry) * -12}px)`,
              }}
            >
              <div style={{
                flexShrink: 0, width: 40, height: 40, borderRadius: 10,
                background: palette.accent, display: "flex", alignItems: "center", justifyContent: "center",
                transform: `scale(${0.7 + 0.3 * iconPop})`,
              }}>
                <TrustIcon name={b.icon} color="#FFFFFF" />
              </div>
              <div style={{ display: "flex", flexDirection: "column" }}>
                <span style={{ fontFamily: headingFont, fontSize: 19, fontWeight: 800, color: palette.ink }}>
                  {b.primary}
                </span>
                <span style={{
                  fontFamily: labelFont, fontSize: 11.5, fontWeight: 600, letterSpacing: 1,
                  color: palette.inkSoft, textTransform: "uppercase",
                }}>
                  {b.secondary}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
