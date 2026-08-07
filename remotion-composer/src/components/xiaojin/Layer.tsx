import React from "react";

/**
 * Thin z-index wrapper for the Phase E "layers" feature — lets a content
 * card's stacking order be user-editable (via the item's own optional
 * `layer` field) without touching each of the ~22 card components
 * individually, and without disturbing the two documented, confirmed bug
 * fixes in XiaojinEditorial.tsx's own paint order (content always above
 * SpeakerCard; outro always below qrContact). Those two orderings are kept
 * safe STRUCTURALLY, not by convention: every content card's z-index lives
 * in the fixed 100-199 band below, which can never overlap the facecam
 * band (10) or the close/intro/chrome bands (300+) above it, regardless of
 * what `layer` value a user picks.
 */
export const Layer: React.FC<{ z: number; children: React.ReactNode }> = ({ z, children }) => (
  <div style={{ position: "absolute", inset: 0, zIndex: z }}>{children}</div>
);

const CONTENT_BAND_MIN = 100;
const CONTENT_BAND_MAX = 199;

/**
 * Effective z-index for one content-zone card item. `layer` (0-99, from the
 * item's own optional schema field) overrides `defaultLayer` (this
 * section's position in XiaojinEditorial.tsx's own historical paint order —
 * see DEFAULT_CONTENT_LAYER below). Absent `layer` on every item in a job
 * reproduces today's exact stacking order, since DEFAULT_CONTENT_LAYER's
 * values are already monotonically increasing in that same order.
 */
export function contentZ(layer: number | undefined, defaultLayer: number): number {
  const raw = typeof layer === "number" ? layer : defaultLayer;
  return CONTENT_BAND_MIN + Math.min(CONTENT_BAND_MAX - CONTENT_BAND_MIN, Math.max(0, raw));
}

/**
 * Each content section's position in XiaojinEditorial.tsx's ORIGINAL
 * (pre-Phase-E) JSX order — the exact sequence content cards have always
 * painted in. Ascending values here reproduce that exact order when no
 * item anywhere sets an explicit `layer`.
 */
export const DEFAULT_CONTENT_LAYER: Record<string, number> = {
  zoneHeaders: 0,
  quotes: 1,
  dataCards: 2,
  beforeAfter: 3,
  gauges: 4,
  countdowns: 5,
  calendarEvents: 6,
  pills: 7,
  stepLists: 8,
  topicCards: 9,
  comparisons: 10,
  rankedLists: 11,
  checklists: 12,
  locationPins: 13,
  testimonials: 14,
  iconClusters: 15,
  progressBars: 16,
  prosCons: 17,
  milestoneTracks: 18,
  trustBadges: 19,
  barCharts: 20,
  milestoneUnlocks: 21,
};
