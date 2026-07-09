/**
 * Section icon set — generalized from video-studio's per-project graphics
 * (vell-renewal-reminder/src/VellRenewal/ShieldGraphic.tsx et al).
 *
 * These are the "bespoke section graphics" the style codex uses to avoid
 * text-only sections. Each icon is a pure stroke-style SVG in the template's
 * accent color, with the same entrance animation contract: `localFrame`
 * counts from the moment the icon mounts; scale 0.86→1 over 26f, fade over
 * 18f (APPLE easing) — identical to the hand-built reference so a ported
 * section is indistinguishable from the original.
 *
 * Adding an icon: draw in a ~200x238 viewBox, stroke-only, register it in
 * SECTION_ICONS. Content-side selection happens in content_planner (P2),
 * which may only emit names present in this registry (contract-② enum).
 */
import { interpolate } from "remotion";
import { APPLE } from "./theme";

export interface SectionIconProps {
  localFrame: number;
  color: string;
}

const useEntrance = (localFrame: number) => ({
  scale: interpolate(localFrame, [0, 26], [0.86, 1.0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
  }),
  opacity: interpolate(localFrame, [0, 18], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
  }),
});

const svgStyle = (scale: number, opacity: number): React.CSSProperties => ({
  opacity,
  transform: `scale(${scale})`,
  transformOrigin: "center center",
  overflow: "visible",
});

/** Shield with centre checkmark — protection / coverage / guarantee topics. */
export const ShieldCheckIcon: React.FC<SectionIconProps> = ({ localFrame, color }) => {
  const { scale, opacity } = useEntrance(localFrame);
  // Checkmark draws on after the shield lands (stroke-dash reveal)
  const checkDraw = interpolate(localFrame, [16, 40], [120, 0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
  });
  return (
    <svg width={220} height={262} viewBox="0 0 200 238" style={svgStyle(scale, opacity)}>
      <path
        d="M 100,6 C 152,6 194,28 194,28 L 194,136 C 194,190 100,232 100,232 C 100,232 6,190 6,136 L 6,28 C 6,28 48,6 100,6 Z"
        fill="none" stroke={color} strokeWidth="5.5" strokeLinejoin="round"
      />
      <path
        d="M 100,22 C 145,22 178,40 178,40 L 178,136 C 178,178 100,212 100,212 C 100,212 22,178 22,136 L 22,40 C 22,40 55,22 100,22 Z"
        fill="none" stroke={color} strokeWidth="1.8" strokeLinejoin="round" opacity={0.4}
      />
      <polyline
        points="68,118 90,142 136,92"
        fill="none" stroke={color} strokeWidth="7"
        strokeLinecap="round" strokeLinejoin="round" opacity={0.85}
        strokeDasharray={120} strokeDashoffset={checkDraw}
      />
    </svg>
  );
};

/** Rounded triangle with exclamation — risk / warning / consequence topics. */
export const WarningIcon: React.FC<SectionIconProps> = ({ localFrame, color }) => {
  const { scale, opacity } = useEntrance(localFrame);
  // Exclamation pulses once as it lands
  const pulse = interpolate(localFrame, [20, 32, 44], [1, 1.12, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <svg width={230} height={230} viewBox="0 0 220 220" style={svgStyle(scale, opacity)}>
      <path
        d="M 110,14 C 116,14 121,17 124,22 L 204,168 C 210,178 203,192 191,192 L 29,192 C 17,192 10,178 16,168 L 96,22 C 99,17 104,14 110,14 Z"
        fill="none" stroke={color} strokeWidth="5.5" strokeLinejoin="round"
      />
      <g transform={`translate(110 118) scale(${pulse}) translate(-110 -118)`}>
        <line x1="110" y1="70" x2="110" y2="132" stroke={color} strokeWidth="8" strokeLinecap="round" opacity={0.85} />
        <circle cx="110" cy="160" r="6.5" fill={color} opacity={0.85} />
      </g>
    </svg>
  );
};

/** Clock face with sweeping hand — deadline / time-pressure topics. */
export const ClockIcon: React.FC<SectionIconProps> = ({ localFrame, color }) => {
  const { scale, opacity } = useEntrance(localFrame);
  const sweep = interpolate(localFrame, [10, 70], [0, 300], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
  });
  return (
    <svg width={230} height={230} viewBox="0 0 220 220" style={svgStyle(scale, opacity)}>
      <circle cx="110" cy="110" r="92" fill="none" stroke={color} strokeWidth="5.5" />
      <circle cx="110" cy="110" r="80" fill="none" stroke={color} strokeWidth="1.8" opacity={0.4} />
      {/* hour hand fixed, minute hand sweeps */}
      <line x1="110" y1="110" x2="110" y2="66" stroke={color} strokeWidth="7" strokeLinecap="round" opacity={0.85}
        transform={`rotate(${sweep} 110 110)`} />
      <line x1="110" y1="110" x2="142" y2="110" stroke={color} strokeWidth="7" strokeLinecap="round" opacity={0.6} />
      <circle cx="110" cy="110" r="7" fill={color} />
    </svg>
  );
};

export const SECTION_ICONS: Record<string, React.FC<SectionIconProps>> = {
  shield_check: ShieldCheckIcon,
  warning: WarningIcon,
  clock: ClockIcon,
};

export type SectionIconName = keyof typeof SECTION_ICONS;
