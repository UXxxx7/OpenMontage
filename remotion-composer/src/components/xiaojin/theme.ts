/**
 * Shared visual tokens for the xiaojin-editorial composition family.
 * Ported from VeLL-lab/video-studio's `motion/vell-renewal-reminder/src/theme.ts`,
 * generalized to support both color modes documented in that project's
 * CLAUDE-xiaojin-editorial.md (warm/dark), instead of one hardcoded palette.
 */
import { Easing } from "remotion";

export const W = 1080;
export const H = 1920;
export const FPS = 30;

export type ColorMode = "warm" | "dark";

export interface XiaojinPalette {
  bg: string;
  bgDeep: string;
  card: string;
  cardAlt: string;
  ink: string;
  inkSoft: string;
  accent: string;
  accentAlt: string;
  good: string;
  bad: string;
  line: string;
  shadow: string;
  captionBg: string;
  captionText: string;
  captionHighlight: string;
}

export const PALETTES: Record<ColorMode, XiaojinPalette> = {
  warm: {
    bg: "#F2EBE0",
    bgDeep: "#E7DCC9",
    card: "#FFFFFF",
    cardAlt: "#F8F4ED",
    ink: "#2A2620",
    inkSoft: "#7A7060",
    accent: "#C4714A", // terracotta
    accentAlt: "#E0552F", // orange-red
    good: "#4F9D69",
    bad: "#CF5448",
    line: "rgba(42,38,32,0.10)",
    shadow: "rgba(60,45,30,0.22)",
    captionBg: "rgba(0,0,0,0.55)",
    captionText: "#FFFFFF",
    captionHighlight: "#E0552F",
  },
  dark: {
    bg: "#0D1117",
    bgDeep: "#090C10",
    card: "#161B22",
    cardAlt: "#1C2129",
    ink: "#F0F2F5",
    inkSoft: "#9AA4B2",
    accent: "#4D9EFF",
    accentAlt: "#7CB8FF",
    good: "#4F9D69",
    bad: "#E0554A",
    line: "rgba(255,255,255,0.10)",
    shadow: "rgba(0,0,0,0.45)",
    captionBg: "transparent",
    captionText: "#4D9EFF",
    captionHighlight: "#7CB8FF",
  },
};

// Pinned chrome zones — these never move, and other elements must respect
// them (see CLAUDE-v2.md's pre-render checklist this ports from).
export const NAV = { h: 88 };
export const COMPLIANCE = { y: 1824, h: 88 };
// Lighter, non-regulatory equivalent of COMPLIANCE — used by BrandBar for
// content that isn't under a disclosure requirement (ported from
// video-studio's chris-quote/iman-watches/retirement-fund theme.ts, which
// all use this exact zone for their BrandBar).
export const BRAND = { y: 1848, h: 64 };
export const PROGRESS = { y: 1912, h: 8 };
export const CAPTION_BOTTOM = 90;

// Apple-style easing — smooth, no overshoot. Used for all card travel.
export const APPLE = Easing.bezier(0.25, 0.1, 0.25, 1.0);

export const RAINBOW =
  "linear-gradient(90deg,#E0552F,#E8A13C,#5FA86B,#4D9EFF,#8C6BD8)";
