/**
 * Circular countdown ring — "Countdown days" row of compose-director.md's
 * Data Display Analysis table ("Circular countdown ring, arc filling to show
 * urgency").
 *
 * Ported from video-studio's motion/vell-renewal-fresh/src/RenewalFresh/IntroSection.tsx
 * (lines 27-108) — generalized: the number/unit/headline text are now props
 * instead of hardcoded "30"/"DAYS"/"Your policy expires soon".
 *
 * Note carried over from the original (documented here rather than silently
 * "fixed", since it may be the intended look): the ring's fill always reveals
 * to 100% regardless of the number shown — it's a pure entrance animation, not
 * `value/maxValue`. If a caller wants the fill to actually represent urgency
 * proportionally, pass `maxValue` and the fill will use `value/maxValue`
 * instead; omit it to keep the original's simple full-reveal behavior.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { APPLE, ColorMode, PALETTES } from "./theme";

export interface CountdownRingProps {
  value: number;
  unitLabel: string;
  /** Small label above the headline, e.g. "UNTIL RENEWAL". */
  label: string;
  headline: string;
  /** Rendered on its own line in the accent color, below `headline`. */
  headlineAccent?: string;
  /** If given, ring fill = value/maxValue (clamped 0-1). Omit for a full reveal regardless of value. */
  maxValue?: number;
  mountFrame: number;
  revealFrames?: number;
  /** Frame this card starts fading out (15-frame fade). Omit to stay on screen for the rest of the video. */
  endFrame?: number;
  x?: number;
  y?: number;
  /** Fixed card width (e.g. the full 960px content-zone lane). Omit for content-sized. */
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const CountdownRing: React.FC<CountdownRingProps> = ({
  value,
  unitLabel,
  label,
  headline,
  headlineAccent,
  maxValue,
  mountFrame,
  revealFrames = 40,
  endFrame,
  x = 80,
  y = 900,
  width,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;
  if (endFrame !== undefined && frame >= endFrame) return null;
  const exitFade =
    endFrame !== undefined
      ? interpolate(frame, [endFrame - 15, endFrame], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
      : 1;

  const palette = PALETTES[colorMode];
  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 220 } });

  const R = 100;
  const CIRC = 2 * Math.PI * R;
  const targetFill = maxValue ? Math.max(0, Math.min(1, value / maxValue)) : 1;
  const arcFill = interpolate(local, [0, revealFrames], [0, targetFill], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: APPLE,
  });
  const dashOffset = CIRC * (1 - arcFill);

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        opacity: cardEntry * exitFade,
        transform: `translateX(${(1 - cardEntry) * -40}px)`,
        background: "rgba(13,17,23,0.86)",
        borderRadius: 24,
        padding: "32px 36px",
        boxShadow: `0 8px 32px ${palette.shadow}`,
        display: "flex",
        alignItems: "center",
        // When given a fixed lane width (full content zone), center the
        // ring+text group instead of leaving them hugging the left edge.
        justifyContent: width !== undefined ? "center" : "flex-start",
        width,
        gap: 32,
      }}
    >
      <svg width={R * 2 + 24} height={R * 2 + 24} style={{ flexShrink: 0 }}>
        <circle cx={R + 12} cy={R + 12} r={R} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth={12} />
        <circle
          cx={R + 12}
          cy={R + 12}
          r={R}
          fill="none"
          stroke={palette.accent}
          strokeWidth={12}
          strokeLinecap="round"
          strokeDasharray={CIRC}
          strokeDashoffset={dashOffset}
          transform={`rotate(-90 ${R + 12} ${R + 12})`}
        />
        <text x={R + 12} y={R + 8} textAnchor="middle" fill={palette.accent} fontSize={56} fontWeight={800} fontFamily={headingFont}>
          {value}
        </text>
        <text x={R + 12} y={R + 32} textAnchor="middle" fill="rgba(255,255,255,0.5)" fontSize={16} fontFamily={labelFont}>
          {unitLabel}
        </text>
      </svg>
      <div>
        <div
          style={{
            fontFamily: labelFont,
            fontSize: 16,
            fontWeight: 600,
            color: "rgba(255,255,255,0.5)",
            letterSpacing: 3,
            textTransform: "uppercase",
            marginBottom: 10,
          }}
        >
          {label}
        </div>
        <div style={{ fontFamily: headingFont, fontSize: 38, fontWeight: 800, color: "#FFFFFF", lineHeight: 1.2 }}>
          {headline}
          {headlineAccent ? (
            <>
              <br />
              <span style={{ color: palette.accent }}>{headlineAccent}</span>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
};
