import schema from "../../../contracts/render_props.schema.json";
import { isDraggableSection } from "./positioning";

/**
 * Estimated on-screen rectangle for a positioned card item, for the Phase B
 * drag-to-position overlay's selection box.
 *
 * Ported from whatsapp_mvp/props_lint.py's `_rect_height`/`_EST_HEIGHT` —
 * that module is the codebase's own model of how tall each card renders
 * (used server-side, pre-render, for overlap checking); this reuses the
 * same model rather than inventing a second one. There is no shared source
 * of truth between Python and TS for this table — keep both in sync by
 * hand, same situation as src/cuts.ts's own hand-maintained field registry.
 */

const EST_HEIGHT: Record<string, number> = {
  gauge: 330, countdown: 300, calendar: 560, beforeAfter: 330,
  pill: 100, zoneHeader: 130, topicCard: 180, qrContact: 260,
  locationPin: 190, testimonial: 220,
  progressBar: 190, milestoneTrack: 210, barChart: 360, milestoneUnlock: 240,
  // Not a content card — the drag box for where captions render (see
  // Captions.tsx). ~160px covers one wrapped line at the component's own
  // default fontSize/padding/lineHeight; height isn't itself draggable
  // (matches every other section here — width-only resize), so this is
  // purely the box's estimated on-screen size, not a stored field.
  captionPosition: 160,
};

// Schema section name -> the "kind" key props_lint.py's own tables use.
const SECTION_KIND: Record<string, string> = {
  dataCards: "dataCard", gauges: "gauge", countdowns: "countdown",
  calendarEvents: "calendar", beforeAfter: "beforeAfter", pills: "pill",
  stepLists: "stepList", topicCards: "topicCard", zoneHeaders: "zoneHeader",
  comparisons: "comparison", rankedLists: "rankedList", checklists: "checklist",
  locationPins: "locationPin", testimonials: "testimonial", iconClusters: "iconCluster",
  progressBars: "progressBar", prosCons: "prosCons", milestoneTracks: "milestoneTrack",
  trustBadges: "trustBadge", barCharts: "barChart", milestoneUnlocks: "milestoneUnlock",
};

function rectHeight(kind: string, entry: Record<string, unknown>): number {
  const arrLen = (v: unknown) => (Array.isArray(v) ? v.length : 0);
  switch (kind) {
    case "dataCard":
      return 100 + 112 * arrLen(entry.rows);
    case "stepList":
      return 40 + 95 * arrLen(entry.steps);
    case "comparison": {
      const cols = (entry.columns as { items?: unknown[] }[] | undefined) ?? [];
      const maxItems = cols.reduce((m, c) => Math.max(m, arrLen(c.items)), 0);
      return 60 + 40 * maxItems;
    }
    case "rankedList":
      return 50 + 68 * arrLen(entry.items);
    case "checklist":
      return 30 + 68 * arrLen(entry.items);
    case "iconCluster":
      return 70 + 60 * Math.ceil(arrLen(entry.items) / 3);
    case "prosCons":
      return 50 + 34 * Math.max(arrLen(entry.pros), arrLen(entry.cons));
    case "trustBadge":
      return 30 + 68 * arrLen(entry.badges);
    default:
      return EST_HEIGHT[kind] ?? 320;
  }
}

function schemaDefaultWidth(section: string): number {
  const schemaAny = schema as unknown as {
    properties?: Record<string, { items?: { properties?: Record<string, { default?: number }> } }>;
  };
  return schemaAny.properties?.[section]?.items?.properties?.width?.default ?? 960;
}

export interface Rect { x: number; y: number; w: number; h: number }

/** Null when the item has no x/y (full-canvas or chrome-anchored types). */
export function estimatedRect(section: string, item: Record<string, unknown>): Rect | null {
  if (typeof item.x !== "number" || typeof item.y !== "number") return null;
  const kind = SECTION_KIND[section] ?? section;
  const w = typeof item.width === "number" ? item.width : schemaDefaultWidth(section);
  return { x: item.x, y: item.y, w, h: rectHeight(kind, item) };
}

export const NATIVE_W = 1080;
export const NATIVE_H = 1920;

/**
 * Where a freshly-added item (no x/y yet — see App.tsx's handleAddItem)
 * should land: centered on the canvas. Returns null for non-"xy" sections
 * (nothing to center — e.g. presenter/scenes aren't plain per-item boxes).
 *
 * Uses the SAME width/height model estimatedRect does (schemaDefaultWidth,
 * rectHeight/SECTION_KIND) so a card's estimated box and its centered
 * position always agree — call this AFTER the item's own content arrays
 * (rows/steps/items/…) are populated, since rectHeight reads those.
 */
export function centeredPosition(section: string, item: Record<string, unknown>): { x: number; y: number } | null {
  if (!isDraggableSection(section)) return null;
  const kind = SECTION_KIND[section] ?? section;
  const w = typeof item.width === "number" ? item.width : schemaDefaultWidth(section);
  const h = rectHeight(kind, item);
  const x = Math.max(0, Math.round((NATIVE_W - w) / 2));
  const y = Math.max(0, Math.round((NATIVE_H - h) / 2));
  return { x, y };
}
