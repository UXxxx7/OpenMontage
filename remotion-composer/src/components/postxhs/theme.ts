/**
 * post-xhs style theme — canonical spec from VeLL-lab/video-studio's
 * tools/directors/compose-director.md ("Compose Director — xiaojin-editorial
 * Style Codex"). This supersedes the earlier "pre-xhs" layout ported into
 * this repo's sibling `xiaojin/` component set (Dominant/Workflow card
 * modes replace the old 5-scene hardcoded schedule; captions are 48px with
 * an always-on pill; there is only one color mode — cream — not warm/dark).
 */
import { Easing } from "remotion";

export const W = 1080;
export const H = 1920;

export const COLOR = {
  cream: "#F2EBE0",
  creamDeep: "#E7DCC9",
  card: "#FFFFFF",
  ink: "#2A2620",
  inkSoft: "#7A7060",
  terracotta: "#C4714A",
  orange: "#E0552F",
  blue: "#4D9EFF",
  good: "#4F9D69",
  bad: "#CF5448",
  line: "rgba(42,38,32,0.10)",
  shadow: "rgba(60,45,30,0.22)",
} as const;

// Pinned chrome zones — content MUST NOT enter these.
export const NAV = { h: 88 };
export const BRAND = { y: 1848, h: 64 };
export const PROG = { y: 1912, h: 8 };
export const CAPTION_BOTTOM = 90;

// Speaker card modes — both ratio 0.565 by construction, so switching
// between them is a clean scale, not a reshape (see compose-director.md's
// "why the ratio changed" note).
export const DOMINANT = { x: 60, y: 100, w: 960, h: 1700 }; // bottom=1800
export const WORKFLOW = { x: 740, y: 1200, w: 300, h: 531 }; // bottom=1731
export const CARD_RATIO = 0.565;

export const APPLE = Easing.bezier(0.25, 0.1, 0.25, 1.0);

export const RAINBOW =
  "linear-gradient(90deg,#E0552F,#E8A13C,#5FA86B,#4D9EFF,#8C6BD8)";
