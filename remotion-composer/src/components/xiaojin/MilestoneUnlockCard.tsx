/**
 * MilestoneUnlockCard — a punchier, achievement-style reveal for a single
 * big number ("1,000+ Families Protected"), reserved for genuine scale
 * moments. Distinct from a plain count_up row (InfoCard): this is solo,
 * full-width, with a spring-popped badge icon instead of a data-card label
 * row — the dramatic version, not the routine one.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export type UnlockIconName = "award" | "star" | "heart" | "bolt";

const ICON_PATHS: Record<UnlockIconName, React.ReactNode> = {
  award: (
    <>
      <circle cx="12" cy="8.5" r="6" />
      <path d="M8.5 13.8 7 22l5-2.8 5 2.8-1.5-8.2" />
    </>
  ),
  star: <path d="M12 2.5 14.9 9l7 .8-5.2 4.7 1.5 6.8-6.2-3.6-6.2 3.6 1.5-6.8-5.2-4.7 7-.8 3.1-6.5Z" />,
  heart: <path d="M12 21s-7.5-4.6-10-9.2C.4 8 2 4.5 5.5 4c2-.3 3.9.6 6.5 3 2.6-2.4 4.5-3.3 6.5-3C22 4.5 23.6 8 22 11.8 19.5 16.4 12 21 12 21Z" />,
  bolt: <path d="M13 2 4 14h6l-2 8 10-13h-6l1-7Z" />,
};

export interface MilestoneUnlockCardProps {
  value: number;
  /** Appended after the counted-up value, e.g. "+" for "1,000+". */
  suffix?: string;
  prefix?: string;
  label: string;
  icon?: UnlockIconName;
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const MilestoneUnlockCard: React.FC<MilestoneUnlockCardProps> = ({
  value,
  suffix = "",
  prefix = "",
  label,
  icon = "award",
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
  const badgePop = spring({ frame: local, fps, config: { damping: 10, stiffness: 220 } });
  const numberFade = interpolate(local, [10, 22], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const shownValue = interpolate(local, [10, 40], [0, value], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        background: `linear-gradient(160deg, ${palette.card}, ${palette.cardAlt})`,
        borderRadius: 24, padding: "36px 24px", textAlign: "center",
        boxShadow: `0 10px 36px ${palette.shadow}`,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
      }}
    >
      <div style={{
        width: 72, height: 72, margin: "0 auto 18px", borderRadius: "50%",
        background: `linear-gradient(135deg, ${palette.accent}, ${palette.accentAlt})`,
        display: "flex", alignItems: "center", justifyContent: "center",
        boxShadow: `0 10px 24px ${palette.shadow}`,
        transform: `scale(${badgePop})`,
      }}>
        <svg width={32} height={32} viewBox="0 0 24 24" fill="#FFFFFF" stroke="#FFFFFF" strokeWidth={0.5}>
          {ICON_PATHS[icon]}
        </svg>
      </div>
      <div style={{
        fontFamily: headingFont, fontSize: 44, fontWeight: 900, color: palette.ink,
        letterSpacing: -0.5, opacity: numberFade,
      }}>
        {prefix}{Math.round(shownValue).toLocaleString()}{suffix}
      </div>
      <div style={{
        fontFamily: labelFont, fontSize: 15, fontWeight: 700, letterSpacing: 1.5,
        textTransform: "uppercase", color: palette.inkSoft, marginTop: 6, opacity: numberFade,
      }}>
        {label}
      </div>
    </div>
  );
};
