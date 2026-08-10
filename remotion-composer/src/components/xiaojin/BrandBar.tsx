/**
 * Lighter, non-regulatory bottom strip — company name + small label.
 * This is the counterpart to ComplianceBar for content that isn't under a
 * disclosure requirement (e.g. a brand style-study video, not an insurance
 * disclosure). Use exactly one of BrandBar / ComplianceBar per build, never
 * both — they occupy overlapping bottom zones (see theme.ts's BRAND vs
 * COMPLIANCE constants).
 *
 * Ported from video-studio's chris-quote/iman-watches/retirement-fund
 * BrandBar.tsx (identical across all three except the two text fields),
 * generalized to take them as props.
 */
import { BRAND, ColorMode, PALETTES, W } from "./theme";

export interface BrandBarProps {
  company: string;
  label: string;
  colorMode?: ColorMode;
  font?: string;
  /** Editor-only position override — omit for the default pinned-bottom,
   *  full-width placement (BRAND.y / W from theme.ts). */
  x?: number;
  y?: number;
  width?: number;
}

export const BrandBar: React.FC<BrandBarProps> = ({
  company,
  label,
  colorMode = "warm",
  font = "inherit",
  x = 0,
  y = BRAND.y,
  width = W,
}) => {
  const palette = PALETTES[colorMode];
  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        height: BRAND.h,
        background: colorMode === "warm" ? "rgba(242,235,224,0.92)" : "rgba(13,17,23,0.92)",
        borderTop: `1px solid ${palette.line}`,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
      }}
    >
      <span
        style={{
          fontFamily: font,
          fontSize: 15,
          fontWeight: 800,
          letterSpacing: 2,
          color: palette.ink,
        }}
      >
        {company}
      </span>
      <span style={{ width: 4, height: 4, borderRadius: 2, background: palette.accent }} />
      <span
        style={{
          fontFamily: font,
          fontSize: 13,
          fontWeight: 500,
          letterSpacing: 1,
          color: palette.inkSoft,
        }}
      >
        {label}
      </span>
    </div>
  );
};
