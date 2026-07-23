/**
 * TestimonialCard — compact avatar + name/role + short quoted line. Distinct
 * from the existing full-canvas `quote` type (QuoteCard: typographic,
 * speaker-hiding, one dramatic line): this is a small social-proof card that
 * sits in the content zone like any other beat, for a THIRD PARTY's words
 * ("my client told me...", a review, a colleague's remark) rather than the
 * main speaker's own quoted line.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface TestimonialCardProps {
  quote: string;
  name: string;
  role?: string;
  /** Initials shown in the avatar circle when no photo is available. */
  initials?: string;
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const TestimonialCard: React.FC<TestimonialCardProps> = ({
  quote,
  name,
  role,
  initials,
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
  const avatarPop = spring({ frame: Math.max(0, local - 6), fps, config: { damping: 12, stiffness: 260 } });
  const initialsText = initials ?? name.slice(0, 1).toUpperCase();

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        background: palette.card, borderRadius: 20,
        padding: "30px 32px", boxShadow: `0 8px 32px ${palette.shadow}`,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
      }}
    >
      <svg width={36} height={28} viewBox="0 0 36 28" fill="none" style={{ marginBottom: 10, opacity: 0.35 }}>
        <path
          d="M0 16C0 6 6 0 15 0v6c-5 1-8 4-8 9h8v13H0V16Zm21 0C21 6 27 0 36 0v6c-5 1-8 4-8 9h8v13H21V16Z"
          fill={palette.accent}
        />
      </svg>
      <p style={{
        fontFamily: labelFont, fontSize: 24, fontWeight: 600, color: palette.ink,
        lineHeight: 1.4, margin: "0 0 22px",
      }}>
        {quote}
      </p>
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <div style={{
          width: 52, height: 52, borderRadius: "50%", flexShrink: 0,
          background: palette.accent, display: "flex", alignItems: "center", justifyContent: "center",
          transform: `scale(${0.7 + 0.3 * avatarPop})`,
        }}>
          <span style={{ fontFamily: headingFont, fontSize: 20, fontWeight: 800, color: "#FFFFFF" }}>
            {initialsText}
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column" }}>
          <span style={{ fontFamily: headingFont, fontSize: 18, fontWeight: 800, color: palette.ink }}>
            {name}
          </span>
          {role ? (
            <span style={{ fontFamily: labelFont, fontSize: 13, fontWeight: 500, color: palette.inkSoft }}>
              {role}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
};
