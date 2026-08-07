import { itemTimeRange } from "./model";
import { humanLabel } from "../fieldHints";
import type { VideoCut } from "../../src/cuts";

/**
 * Timeline's "Layers" view model — an alternative grouping of the SAME 22
 * content-zone sections buildLanes() already covers (state/model.ts), this
 * time bucketed by stacking order (the item's own `layer` field) instead of
 * by section/type. Mirrors src/components/xiaojin/Layer.tsx's
 * DEFAULT_CONTENT_LAYER exactly, by hand — same situation as src/cuts.ts's
 * own hand-maintained field registry, and the same reason: kept in one
 * small, explicit table rather than derived, since "is this section part of
 * the z-order band" isn't something the schema alone can answer (chapters/
 * scenes/captions/etc are legitimately NOT part of it).
 */
const DEFAULT_CONTENT_LAYER: Record<string, number> = {
  zoneHeaders: 0, quotes: 1, dataCards: 2, beforeAfter: 3, gauges: 4,
  countdowns: 5, calendarEvents: 6, pills: 7, stepLists: 8, topicCards: 9,
  comparisons: 10, rankedLists: 11, checklists: 12, locationPins: 13,
  testimonials: 14, iconClusters: 15, progressBars: 16, prosCons: 17,
  milestoneTracks: 18, trustBadges: 19, barCharts: 20, milestoneUnlocks: 21,
};

export function isLayeredSection(section: string): boolean {
  return section in DEFAULT_CONTENT_LAYER;
}

export function effectiveLayer(section: string, item: Record<string, unknown>): number {
  const raw = typeof item.layer === "number" ? item.layer : DEFAULT_CONTENT_LAYER[section] ?? 0;
  return Math.min(99, Math.max(0, Math.round(raw)));
}

export type LayerClipItem = {
  section: string;
  index: number;
  from: number;
  to: number;
  openEnded: boolean;
  isPoint: boolean;
  label: string;
  layer: number;
};

export type LayerRow = {
  layer: number;
  items: LayerClipItem[];
};

// Matches state/model.ts's own LABEL_KEYS plus eventLabel (calendarEvents'
// own field — model.ts's list predates this file and has the same gap,
// but that view shows a per-section lane label alongside each clip so it's
// less noticeable there; this view has no section label, so a good
// per-item label matters more).
const LABEL_KEYS = ["title", "label", "headline", "text", "heading", "name", "eventLabel"];

function labelFor(item: Record<string, unknown>, section: string): string {
  for (const k of LABEL_KEYS) {
    const v = item[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return humanLabel(section);
}

/**
 * One row per layer number from 0 to the highest layer actually in use
 * (never fewer than 1 row, so an empty job still has a "+ Add row" target).
 * `extraEmptyRows` renders additional trailing empty rows past the highest
 * used layer — the "+ Add row" affordance is purely visual (see
 * Timeline.tsx): there is nothing to persist for an empty row, it just
 * stops being rendered on reload, same as any other derived-view scratch state.
 */
export function buildLayerRows(
  props: Record<string, unknown>,
  durationInFrames: number,
  cuts: readonly VideoCut[],
  extraEmptyRows = 0
): LayerRow[] {
  const byLayer = new Map<number, LayerClipItem[]>();

  for (const section of Object.keys(DEFAULT_CONTENT_LAYER)) {
    const arr = props[section];
    if (!Array.isArray(arr)) continue;
    arr.forEach((rawItem, index) => {
      if (!rawItem || typeof rawItem !== "object") return;
      const item = rawItem as Record<string, unknown>;
      const range = itemTimeRange(section, item, durationInFrames, cuts);
      if (!range) return;
      const layer = effectiveLayer(section, item);
      const clip: LayerClipItem = {
        section, index, layer,
        from: range.from, to: range.to,
        openEnded: range.openEnded, isPoint: range.isPoint,
        label: labelFor(item, section),
      };
      if (!byLayer.has(layer)) byLayer.set(layer, []);
      byLayer.get(layer)!.push(clip);
    });
  }

  const maxUsed = byLayer.size > 0 ? Math.max(...byLayer.keys()) : -1;
  const highestRow = Math.max(maxUsed, extraEmptyRows > 0 ? maxUsed + extraEmptyRows : 0);
  const rows: LayerRow[] = [];
  for (let l = 0; l <= highestRow; l++) {
    const items = (byLayer.get(l) || []).sort((a, b) => a.from - b.from);
    rows.push({ layer: l, items });
  }
  return rows;
}
