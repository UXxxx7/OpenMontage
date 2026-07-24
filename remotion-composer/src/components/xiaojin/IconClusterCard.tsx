/**
 * IconClusterCard — a staggered grid of icon+label chips for a moment that
 * namechecks several related concepts at once with no hard numbers (e.g.
 * "works with WhatsApp, Instagram, and YouTube"). Distinct from StepList
 * (ordered, sequential) and TopicCard (one single statement): this is for
 * an unordered SET of things mentioned together.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export type ClusterIconName = "chat" | "camera" | "play" | "star" | "bolt" | "heart";

const ICON_PATHS: Record<ClusterIconName, React.ReactNode> = {
  chat: <path d="M4 5a3 3 0 0 1 3-3h14a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3h-9l-5 4v-4h-1a3 3 0 0 1-3-3V5Z" />,
  camera: (
    <>
      <rect x="3" y="7" width="18" height="13" rx="2.5" />
      <circle cx="12" cy="13.5" r="3.6" />
      <path d="M8.5 7 10 4h4l1.5 3" />
    </>
  ),
  play: <path d="M6 4.5v15l14-7.5-14-7.5Z" />,
  star: <path d="M12 2.5 14.9 9l7 .8-5.2 4.7 1.5 6.8-6.2-3.6-6.2 3.6 1.5-6.8-5.2-4.7 7-.8 3.1-6.5Z" />,
  bolt: <path d="M13 2 4 14h6l-2 8 10-13h-6l1-7Z" />,
  heart: <path d="M12 21s-7.5-4.6-10-9.2C.4 8 2 4.5 5.5 4c2-.3 3.9.6 6.5 3 2.6-2.4 4.5-3.3 6.5-3C22 4.5 23.6 8 22 11.8 19.5 16.4 12 21 12 21Z" />,
};

const ClusterIcon: React.FC<{ name: ClusterIconName; color: string }> = ({ name, color }) => (
  <svg width={22} height={22} viewBox="0 0 26 26" fill={color} stroke={color} strokeWidth={1.4} strokeLinecap="round" strokeLinejoin="round">
    {ICON_PATHS[name]}
  </svg>
);

export interface IconClusterItem {
  icon: ClusterIconName;
  label: string;
  labelEn?: string;
}

export interface IconClusterCardProps {
  title?: string;
  items: IconClusterItem[];
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
  /** Frames between each chip's pop-in. Default 5. */
  stagger?: number;
}

export const IconClusterCard: React.FC<IconClusterCardProps> = ({
  title,
  items,
  mountFrame,
  endFrame,
  x = 60,
  y = 900,
  width = 960,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
  stagger = 5,
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
    <div style={{ position: "absolute", left: x, top: y, width, opacity: exitFade }}>
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginBottom: 16,
          opacity: cardEntry,
        }}>
          {title}
        </div>
      ) : null}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14 }}>
        {items.map((item, i) => {
          const chipDelay = i * stagger;
          const chipPop = spring({ frame: Math.max(0, local - chipDelay), fps, config: { damping: 13, stiffness: 240 } });
          return (
            <div
              key={i}
              style={{
                display: "flex", alignItems: "center", gap: 12,
                background: palette.card, borderRadius: 999,
                padding: "12px 22px 12px 14px",
                boxShadow: `0 6px 20px ${palette.shadow}`,
                opacity: chipPop,
                transform: `scale(${0.7 + 0.3 * chipPop})`,
              }}
            >
              <div style={{
                width: 36, height: 36, borderRadius: "50%", flexShrink: 0,
                background: `${palette.accent}1F`,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}>
                <ClusterIcon name={item.icon} color={palette.accent} />
              </div>
              <div style={{ display: "flex", flexDirection: "column" }}>
                <span style={{ fontFamily: headingFont, fontSize: 18, fontWeight: 700, color: palette.ink }}>
                  {item.label}
                </span>
                {item.labelEn ? (
                  <span style={{
                    fontFamily: labelFont, fontSize: 10, fontWeight: 600, letterSpacing: 1.2,
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
    </div>
  );
};
