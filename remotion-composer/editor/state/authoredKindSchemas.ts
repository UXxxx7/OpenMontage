import type { ManifestElement } from "./authoredHitTest";
import type { JSONSchema } from "../SchemaForm";

// Mirrors contracts/authored_manifest.schema.json's $defs — the editable
// per-kind CONTENT fields (x/y/w/h are appended to every kind below since
// those are universal, not kind-specific, per the schema's own top-level
// `element` shape). mountFrame/endFrame are ALSO appended to every kind —
// originally excluded here over desync-risk concerns, but both are already
// committed by the timeline's drag-to-retime (AuthoredTimeline.tsx) and
// confirmed working; these just give the same values numeric-precision
// fields alongside the drag. `isFrameField` (fieldHints.ts) already matches
// `.*Frame$`, so SchemaForm renders them with the same FrameInput widget
// Arm A uses for its own frame fields — no SchemaForm change needed.
//
// Extracted from AuthoredEditor.tsx (Phase 5, phone shell) so
// AuthoredInspectorBody.tsx can share it without a circular import between
// the desktop shell and the inspector content it now also hosts inside a
// PhoneSheet.
export const KIND_SCHEMAS: Record<ManifestElement["kind"], JSONSchema> = {
  text_block: {
    type: "object",
    required: ["text"],
    properties: {
      text: { type: "string", maxLength: 200 },
      color: { type: "string" },
      x: { type: "number" }, y: { type: "number" }, w: { type: "number" }, h: { type: "number" },
      mountFrame: { type: "integer", minimum: 0 }, endFrame: { type: "integer", minimum: 0 },
    },
  },
  stat_card: {
    type: "object",
    required: ["headline", "value"],
    properties: {
      headline: { type: "string", maxLength: 80 },
      value: { type: "string" },
      color: { type: "string" },
      x: { type: "number" }, y: { type: "number" }, w: { type: "number" }, h: { type: "number" },
      mountFrame: { type: "integer", minimum: 0 }, endFrame: { type: "integer", minimum: 0 },
    },
  },
  image_swap: {
    type: "object",
    required: ["src"],
    properties: {
      src: { type: "string" },
      x: { type: "number" }, y: { type: "number" }, w: { type: "number" }, h: { type: "number" },
      mountFrame: { type: "integer", minimum: 0 }, endFrame: { type: "integer", minimum: 0 },
    },
  },
  broll_window: {
    type: "object",
    required: ["src"],
    properties: {
      src: { type: "string" },
      label: { type: "string", maxLength: 60 },
      x: { type: "number" }, y: { type: "number" }, w: { type: "number" }, h: { type: "number" },
      mountFrame: { type: "integer", minimum: 0 }, endFrame: { type: "integer", minimum: 0 },
    },
  },
};

export const KIND_LABEL: Record<ManifestElement["kind"], string> = {
  text_block: "Text block",
  stat_card: "Stat card",
  image_swap: "Image",
  broll_window: "B-roll window",
};

/**
 * A `recovered` element (see state/recoverManifest.ts) was synthesized from
 * ids the scene reads via `overrides?.[id]?.field` but that the server's
 * manifest.json never listed — its x/y/w/h are placeholders, not real
 * geometry, because 0 of 18 real generated scenes read position/size from
 * overrides at all. Strip those fields from the schema entirely rather than
 * show a drag/resize control that would silently do nothing.
 */
export function schemaForElement(el: ManifestElement): JSONSchema {
  const base = KIND_SCHEMAS[el.kind];
  if (!el.recovered) return base;
  const { x: _x, y: _y, w: _w, h: _h, ...rest } = base.properties || {};
  return { ...base, properties: rest };
}

export function contentDefaults(el: ManifestElement): Record<string, unknown> {
  const out: Record<string, unknown> = el.recovered ? {} : { x: el.x, y: el.y, w: el.w, h: el.h };
  const fieldNames = Object.keys(schemaForElement(el).properties || {});
  for (const name of fieldNames) {
    if (!(name in out)) out[name] = el[name];
  }
  return out;
}
