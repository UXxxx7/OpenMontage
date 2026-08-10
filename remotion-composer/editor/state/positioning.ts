import schema from "../../../contracts/render_props.schema.json";

/**
 * Which sections carry a draggable position, and what shape that position
 * is — mirrors editor/state/model.ts's TIME_DESCRIPTORS pattern: derive
 * from the schema at module load rather than hand-listing sections, so a
 * new card type added to the schema with plain x/y/width fields becomes
 * draggable with zero code change here.
 */
export type PositionDescriptor =
  /** The ~22 content cards: one item, one independent x/y(+width). */
  | { kind: "xy" }
  /** qrContact: a single top-level OBJECT (index:null selection — see
   *  model.ts's itemAt) but otherwise plain x/y/width, same as "xy". */
  | { kind: "xy-object" }
  /** presenter: a single top-level object with static x/y/w/h — the only
   *  section needing genuine two-axis (w AND h) resize, not just width. */
  | { kind: "xywh" }
  /** scenes[]: a KEYFRAME SCHEDULE (x/y/w/h per entry, interpolated by
   *  SpeakerCard.withHoldKeyframes) — dragging must edit whichever
   *  keyframe is active at the current frame, not treat entries as
   *  independent items. Not implemented in the Phase B overlay yet. */
  | { kind: "keyframed" }
  /** A single position shared by every item in the section (e.g. a future
   *  captionPosition) — no per-item {section,index} selection applies. */
  | { kind: "global" };

function isPlainXYItem(itemSchema: unknown): boolean {
  const props = (itemSchema as { properties?: Record<string, unknown> } | undefined)?.properties;
  return !!props && "x" in props && "y" in props;
}

// Sections whose x/y shape isn't a plain per-item xy — hand-registered
// (like src/cuts.ts's own field registry) because their shape needs
// dedicated handling, not generic derivation.
const HAND_REGISTERED: Record<string, PositionDescriptor> = {
  scenes: { kind: "keyframed" },
  presenter: { kind: "xywh" },
  qrContact: { kind: "xy-object" },
  // compliance/brand/intro are singleton chrome objects with several
  // non-position fields (agentNameZh, variant, …) alongside x/y(/width) —
  // isPlainXYItem's generic array-item derivation doesn't apply to objects
  // at all (see the loop below), so all five here are hand-registered the
  // same way qrContact is. intro's x/y are only meaningful for the "chips"
  // variant — PreviewOverlay's rect resolver (staticElements.ts's own
  // hit()==="backdrop" check) already returns no rect for the other 3
  // variants, so marking the whole section draggable is still safe: a
  // non-chips intro selection simply never produces a rect to drag.
  compliance: { kind: "xy-object" },
  brand: { kind: "xy-object" },
  intro: { kind: "xy-object" },
  // New editor-only position objects — no pipeline field to conflict with,
  // see staticElements.ts/App.tsx for the matching present/default wiring.
  chapterNav: { kind: "xy-object" },
  rainbowBar: { kind: "xy-object" },
};

export const POSITION_DESCRIPTORS: Record<string, PositionDescriptor> = (() => {
  const out: Record<string, PositionDescriptor> = { ...HAND_REGISTERED };
  const props = (schema as { properties?: Record<string, unknown> }).properties ?? {};
  for (const [name, spec] of Object.entries(props)) {
    if (name in HAND_REGISTERED) continue;
    const specAny = spec as { type?: string; items?: unknown };
    if (specAny.type !== "array" || !specAny.items) continue;
    if (isPlainXYItem(specAny.items)) out[name] = { kind: "xy" };
  }
  return out;
})();

export function positionDescriptorFor(section: string): PositionDescriptor | undefined {
  return POSITION_DESCRIPTORS[section];
}

/** Sections draggable by the Phase B overlay today. Every call site that
 *  reads props[section] as an ARRAY already guards with Array.isArray
 *  itself (hitTest.ts's content-card loop, PreviewOverlay's snap targets),
 *  so including the object-shaped kinds here is safe — they simply never
 *  match those array checks and fall through to their own object-aware
 *  handling (PreviewOverlay's item derivation via model.ts's itemAt). */
export function isDraggableSection(section: string): boolean {
  const kind = POSITION_DESCRIPTORS[section]?.kind;
  return kind === "xy" || kind === "xy-object" || kind === "xywh";
}

/** True for sections whose draggable item is a top-level OBJECT
 *  (index:null selection — qrContact, presenter), not an array item. */
export function isObjectDraggableSection(section: string): boolean {
  const kind = POSITION_DESCRIPTORS[section]?.kind;
  return kind === "xy-object" || kind === "xywh";
}

/** True only for the one section (presenter) needing a real w+h resize
 *  handle instead of the usual width-only one. */
export function isTwoAxisResizeSection(section: string): boolean {
  return POSITION_DESCRIPTORS[section]?.kind === "xywh";
}
