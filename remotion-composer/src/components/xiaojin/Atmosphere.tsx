/**
 * Atmosphere — faint drifting keyword texture, generalized from the
 * reference build's hardcoded insurance-terms version
 * (vell-renewal-reminder/src/VellRenewal/Atmosphere.tsx). Background
 * texture, not information: ~6% opacity, slow sinusoidal drift. Gives long
 * talking spans a live surface instead of dead flat background.
 *
 * Keywords come from the planner (this video's own vocabulary — never a
 * canned list). Positions are deterministic from the keyword index so the
 * layout is stable across renders without needing coordinates in the props.
 *
 * [P2 prototype validating the contract-② `atmosphereKeywords` extension;
 * P3 owns the final form.]
 */
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ColorMode, PALETTES } from "./theme";

// Slot grid mirroring the reference's hand-placed layout: alternating
// left/right margins, spread vertically, clear of nav (0-88) and the
// caption/brand band (1690+).
const SLOTS = [
  { x: 60, y: 240 }, { x: 760, y: 300 }, { x: 880, y: 560 }, { x: 70, y: 640 },
  { x: 820, y: 900 }, { x: 50, y: 1020 }, { x: 900, y: 1180 }, { x: 80, y: 1320 },
  { x: 780, y: 1460 }, { x: 120, y: 1560 },
];
const SIZES = [30, 22, 34, 20, 28, 20, 30, 22, 26, 18];

export const Atmosphere: React.FC<{
  keywords: string[];
  colorMode: ColorMode;
  headingFont?: string;
}> = ({ keywords, colorMode, headingFont = "inherit" }) => {
  const frame = useCurrentFrame();
  const palette = PALETTES[colorMode];
  if (!keywords.length) return null;

  return (
    <AbsoluteFill style={{ overflow: "hidden", pointerEvents: "none" }}>
      {SLOTS.map((slot, i) => {
        const term = keywords[i % keywords.length];
        if (!term) return null;
        const drift = Math.sin((frame + i * 40) * 0.012) * 8;
        return (
          <span
            key={i}
            style={{
              position: "absolute",
              left: slot.x,
              top: slot.y + drift,
              fontFamily: headingFont,
              fontSize: SIZES[i],
              fontWeight: 600,
              letterSpacing: /[一-鿿]/.test(term) ? 1 : 2,
              color: palette.ink,
              opacity: 0.06,
              whiteSpace: "nowrap",
            }}
          >
            {term}
          </span>
        );
      })}
    </AbsoluteFill>
  );
};
