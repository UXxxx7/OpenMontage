/**
 * TopicCard — icon + short statement card for spoken supporting lines that
 * don't carry hard data (no number/date/risk to visualize) but still
 * deserve a graphic beat instead of sitting as a plain caption. Fills the
 * "arsenal" gap: a professional, simple popup card for moments with
 * nothing else in the arsenal to reach for.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export type TopicIconName = "chat" | "lightbulb" | "check" | "sparkle";

const ICON_PATHS: Record<TopicIconName, React.ReactNode> = {
  chat: (
    <path d="M6 8a4 4 0 0 1 4-4h20a4 4 0 0 1 4 4v14a4 4 0 0 1-4 4H18l-7 6v-6h-1a4 4 0 0 1-4-4V8Z" />
  ),
  lightbulb: (
    <>
      <path d="M20 4a10 10 0 0 0-6 18v3a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2v-3a10 10 0 0 0-6-18Z" />
      <line x1="16" y1="31" x2="24" y2="31" />
    </>
  ),
  check: (
    <>
      <circle cx="20" cy="20" r="16" />
      <polyline points="13,20 18,25 27,15" />
    </>
  ),
  sparkle: (
    <path d="M20 4 L23 16 L35 20 L23 24 L20 36 L17 24 L5 20 L17 16 Z" />
  ),
};

const TopicIcon: React.FC<{ name: TopicIconName; color: string }> = ({ name, color }) => (
  <svg width={40} height={40} viewBox="0 0 40 40" fill="none" stroke={color} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round">
    {ICON_PATHS[name]}
  </svg>
);

export interface TopicCardProps {
  icon?: TopicIconName;
  headline: string;
  sub?: string;
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const TopicCard: React.FC<TopicCardProps> = ({
  icon = "sparkle",
  headline,
  sub,
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
  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 220 } });

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        background: palette.card, borderRadius: 20,
        padding: "28px 32px",
        boxShadow: `0 8px 32px ${palette.shadow}`,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
        display: "flex", alignItems: "center", gap: 24,
      }}
    >
      <div
        style={{
          width: 72, height: 72, borderRadius: 18, flexShrink: 0,
          background: `${palette.accent}1F`,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}
      >
        <TopicIcon name={icon} color={palette.accent} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span style={{ fontFamily: headingFont, fontSize: 30, fontWeight: 800, color: palette.ink, lineHeight: 1.25 }}>
          {headline}
        </span>
        {sub ? (
          <span style={{ fontFamily: labelFont, fontSize: 18, fontWeight: 500, color: palette.inkSoft }}>
            {sub}
          </span>
        ) : null}
      </div>
    </div>
  );
};
