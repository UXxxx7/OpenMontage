/**
 * LocationPinCard — a stylized map silhouette with a pin dropping onto a
 * named place, for moments referencing a specific location/country/city.
 * Nothing in the existing library visualizes place — every other card is
 * either a number, a claim, or a process step.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface LocationPinCardProps {
  place: string;
  placeEn?: string;
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

export const LocationPinCard: React.FC<LocationPinCardProps> = ({
  place,
  placeEn,
  sub,
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

  // Pin drops from above with an overshoot bounce, lands ~18 frames in;
  // a ripple ring pulses outward once it lands.
  const pinDrop = spring({ frame: Math.max(0, local - 10), fps, config: { damping: 10, stiffness: 180 } });
  const pinY = interpolate(pinDrop, [0, 1], [-60, 0]);
  const landed = local - 10 >= 18;
  const rippleAge = landed ? (local - 28) : 0;
  const rippleScale = interpolate(rippleAge, [0, 26], [0.4, 2.2], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const rippleOpacity = interpolate(rippleAge, [0, 26], [0.5, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        background: palette.card, borderRadius: 20,
        padding: "30px 32px", boxShadow: `0 8px 32px ${palette.shadow}`,
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
        display: "flex", alignItems: "center", gap: 26,
      }}
    >
      <div style={{
        position: "relative", width: 96, height: 96, flexShrink: 0,
        borderRadius: 18, background: palette.bgDeep,
        display: "flex", alignItems: "center", justifyContent: "center", overflow: "hidden",
      }}>
        {/* Faint grid texture reads as "map" without needing real geo data. */}
        <svg width="100%" height="100%" viewBox="0 0 96 96" style={{ position: "absolute", inset: 0 }}>
          {[24, 48, 72].map((v) => (
            <line key={`v${v}`} x1={v} y1={0} x2={v} y2={96} stroke={palette.line} strokeWidth={1} />
          ))}
          {[24, 48, 72].map((h) => (
            <line key={`h${h}`} x1={0} y1={h} x2={96} y2={h} stroke={palette.line} strokeWidth={1} />
          ))}
        </svg>
        <div style={{
          position: "absolute", width: 44, height: 44, borderRadius: "50%",
          border: `2px solid ${palette.accent}`, opacity: rippleOpacity,
          transform: `scale(${rippleScale})`,
        }} />
        <svg
          width={30} height={38} viewBox="0 0 30 38" fill="none"
          style={{ transform: `translateY(${pinY}px)`, position: "relative" }}
        >
          <path
            d="M15 0C6.7 0 0 6.7 0 15c0 10.5 15 23 15 23s15-12.5 15-23C30 6.7 23.3 0 15 0Z"
            fill={palette.accent}
          />
          <circle cx="15" cy="15" r="6" fill={palette.card} />
        </svg>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span style={{ fontFamily: headingFont, fontSize: 30, fontWeight: 800, color: palette.ink, lineHeight: 1.2 }}>
          {place}
        </span>
        {placeEn ? (
          <span style={{
            fontFamily: labelFont, fontSize: 13, fontWeight: 600, letterSpacing: 1.5,
            color: palette.inkSoft, textTransform: "uppercase",
          }}>
            {placeEn}
          </span>
        ) : null}
        {sub ? (
          <span style={{ fontFamily: labelFont, fontSize: 17, fontWeight: 500, color: palette.inkSoft, marginTop: 4 }}>
            {sub}
          </span>
        ) : null}
      </div>
    </div>
  );
};
