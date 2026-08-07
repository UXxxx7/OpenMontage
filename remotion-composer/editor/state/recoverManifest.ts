import type { ManifestElement } from "./authoredHitTest";

// Matches `overrides?.["id"]?.field` (or `.field` without the elvis, or
// single-quoted ids), optionally followed by an `as <Type>)` cast and a
// `?? <default>` fallback — the exact shape scene_author.py's frozen prompt
// mandates (authored_renderer's own worked example:
// `(props.overrides?.["card-1"]?.x as number) ?? 90`). The `?? <default>`
// tail is itself optional so a bare read (no fallback written) still counts
// as a reference, just without a recoverable default value.
//
// The default alternates between a quoted-string branch and a bare-token
// fallback (number/other expression) rather than one catch-all
// `[^;,\n]+` — real scenes routinely default to a dollar string containing
// commas (`"$1,500,000"`), and a naive comma-stop truncates it mid-string.
// The quoted-string branch also tolerates the default sitting on the next
// source line (real scenes wrap `?? \n  "..."` when the property name is
// long) since `\s` spans newlines.
const OVERRIDE_READ_RE =
  /overrides\?\.\[(["'])([^"']+)\1\]\??\.(\w+)(?:\s+as\s+[^)]*)?\)?\s*(?:\?\?\s*((["'`])(?:\\.|(?!\5)[^\\])*\5|[^;,\n]+))?/g;

function parseDefault(raw: string | undefined): unknown {
  if (raw == null) return undefined;
  const trimmed = raw.trim().replace(/[)\s]+$/, "");
  const strMatch = trimmed.match(/^(["'`])((?:\\.|(?!\1)[\s\S])*)\1$/);
  if (strMatch) return strMatch[2];
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  // Not a literal we can parse (an expression, a helper call, etc.) — keep
  // the raw source text. Still useful to show in the Inspector as a string
  // rather than silently dropping the field.
  return trimmed;
}

const KIND_FIELD_HINTS: { kind: ManifestElement["kind"]; requires: string[] }[] = [
  { kind: "stat_card", requires: ["headline", "value"] },
  { kind: "broll_window", requires: ["src", "label"] },
  { kind: "image_swap", requires: ["src"] },
  { kind: "text_block", requires: ["text"] },
];

function inferKind(fields: Set<string>): ManifestElement["kind"] {
  for (const { kind, requires } of KIND_FIELD_HINTS) {
    if (requires.every((f) => fields.has(f))) return kind;
  }
  return "text_block";
}

const GEOMETRY_FIELDS = new Set(["x", "y", "w", "h", "layer"]);

/**
 * scene_author.py's manifest emission has a confirmed 0% reliability floor
 * in production: 2 of 4 real jobs in storage/jobs shipped an empty
 * manifest.json despite their scene.tsx reading
 * `props.overrides?.["id"]?.field` 26-30 times across 5-6 stable ids (traced
 * by hand, not inferred — scene_draft_manifest.json was already `[]` from
 * the model's first response). tsx_validator.py's `_check_manifest_ids_wired`
 * only checks manifest -> src (every listed id must appear in source), never
 * the reverse, so an empty manifest against a fully-wired scene passes
 * validation silently — see tsx_validator.py's new `manifest_incomplete`
 * rule for the server-side half of this fix.
 *
 * This recovers those ids client-side from data the editor already has (the
 * compiled tsx source) — no re-render, no server round-trip, no LLM call.
 * Purely additive: only synthesizes entries for ids the real manifest
 * doesn't already list.
 *
 * Geometry (x/y/w/h/layer) is NEVER recoverable this way and must not be
 * guessed — 0 of 18 real generated scenes read those fields from overrides
 * at all (a separate, confirmed bug: see AuthoredEditor.tsx's own
 * `scenePredatesPositionEditing`), so there is nothing in the source to
 * scan for. Recovered elements get placeholder geometry and are flagged
 * `recovered: true` so the UI can hide position/size controls that would
 * silently do nothing — same principle as the existing banner, applied per
 * element instead of per scene.
 */
export function recoverMissingManifestElements(
  tsx: string,
  existing: ManifestElement[]
): ManifestElement[] {
  const known = new Set(existing.map((e) => e.id));
  const order: string[] = [];
  const byId = new Map<string, Map<string, string | undefined>>();

  OVERRIDE_READ_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = OVERRIDE_READ_RE.exec(tsx))) {
    const id = m[2];
    const field = m[3];
    const defaultRaw = m[4];
    if (known.has(id) || GEOMETRY_FIELDS.has(field)) continue;
    let fields = byId.get(id);
    if (!fields) {
      fields = new Map();
      byId.set(id, fields);
      order.push(id);
    }
    // First occurrence wins — every real scene declares each id's default
    // once, at its first read; a later duplicate read (rare) shouldn't
    // override an already-captured default.
    if (!fields.has(field)) fields.set(field, defaultRaw);
  }

  return order.map((id) => {
    const fields = byId.get(id)!;
    const kind = inferKind(new Set(fields.keys()));
    const el: ManifestElement = {
      id,
      kind,
      layer: 0,
      x: 0,
      y: 0,
      w: 0,
      h: 0,
      recovered: true,
    };
    for (const [field, raw] of fields) {
      const value = parseDefault(raw);
      if (value === undefined) continue;
      if (field === "mountFrame" || field === "endFrame") {
        const n = typeof value === "number" ? value : Number(value);
        if (Number.isFinite(n)) el[field] = n;
      } else {
        el[field] = value;
      }
    }
    // The timeline needs a start frame to place the clip even if the scene
    // never wrote a `?? <default>` fallback for mountFrame (rare, but the
    // regex's tail is optional so it's possible).
    if (typeof el.mountFrame !== "number") el.mountFrame = 0;
    return el;
  });
}

/**
 * Recovers the AUTHORED DEFAULT x/y/w/h a scene wired for one specific
 * reserved override id (e.g. `"__captionBox"`) — the geometry-exclusion in
 * `recoverMissingManifestElements` above deliberately never surfaces x/y/w/h
 * as a normal recovered element (0/18 real scenes wire geometry, so
 * synthesizing fake geometry for a normal element would be actively
 * misleading). This is the opposite case: a caller that KNOWS a specific id
 * is SUPPOSED to have real, hand-wired geometry (because the caller itself
 * arranged for it — e.g. a server-side scene.tsx patch for a caption
 * container) and wants the client's initial drag-box position to match
 * whatever default value that patch actually used, instead of guessing a
 * universal constant that could drift out of sync with the real file.
 * Falls back to `fallback` per-field for any value the scan doesn't find
 * (id not present in this scene at all, or that one field has no `?? `
 * fallback written).
 */
export function parseReservedRectDefaults(
  tsx: string,
  id: string,
  fallback: { x: number; y: number; w: number; h?: number }
): { x: number; y: number; w: number; h?: number } {
  const found = new Map<string, string | undefined>();
  OVERRIDE_READ_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = OVERRIDE_READ_RE.exec(tsx))) {
    if (m[2] !== id) continue;
    const field = m[3];
    if (!"xywh".includes(field) || field.length !== 1) continue;
    if (!found.has(field)) found.set(field, m[4]);
  }
  const num = (field: "x" | "y" | "w" | "h", fallbackVal: number | undefined): number | undefined => {
    const value = parseDefault(found.get(field));
    const n = typeof value === "number" ? value : (typeof value === "string" ? Number(value) : NaN);
    return Number.isFinite(n) ? n : fallbackVal;
  };
  return {
    x: num("x", fallback.x)!,
    y: num("y", fallback.y)!,
    w: num("w", fallback.w)!,
    h: num("h", fallback.h),
  };
}
