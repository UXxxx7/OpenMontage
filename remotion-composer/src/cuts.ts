// Video cuts — the single source of truth for mapping between the SOURCE
// video's own frame numbering and the OUTPUT composition's frame numbering
// once the editor has removed one or more regions ("razor" cuts).
//
// Imported by BOTH `src/` (the actual Remotion composition, for render +
// Player) and `editor/` (the browser timeline). Duration is otherwise easy
// to hand-duplicate and let drift — that already happened once this session
// (calculateXiaojinEditorialMetadata, PreviewPane.tsx and App.tsx each had
// their own copy of the same formula) — so every caller MUST go through
// `computeOutputDuration` here, not recompute it locally.
//
// Model: `videoCuts` is a list of REMOVED regions, half-open [fromFrame,
// toFrame), expressed in the ORIGINAL SOURCE video's own frame numbering —
// never re-based, never relative to a previous cut. Every other frame/ms
// field in `render_props` (captions, chapters, every card's mountFrame,
// etc.) stays in that SAME original source-frame coordinate space forever;
// this module maps it to output-frame space at render/preview time. Nothing
// is ever deleted from props — a card fully inside a cut is dropped only
// from the DERIVED (mapped) array the composition actually renders, not
// from the props the user is editing, so removing the cut brings it back.

export type VideoCut = { fromFrame: number; toFrame: number };

/**
 * Sort, clamp to [0, srcLen], drop empty/invalid entries, and merge
 * overlapping AND touching cuts. Merging touching cuts (not just
 * overlapping ones) matters: leaving [0,100) and [100,200) as two separate
 * cuts would produce a zero-length KEPT segment between them, and
 * Remotion's <Sequence> throws on `durationInFrames <= 0`.
 */
export function normalizeCuts(
  cuts: readonly VideoCut[] | undefined | null,
  srcLen: number
): VideoCut[] {
  if (!cuts || cuts.length === 0) return [];
  const clamped: VideoCut[] = [];
  for (const c of cuts) {
    if (!c || typeof c.fromFrame !== "number" || typeof c.toFrame !== "number") continue;
    if (!Number.isFinite(c.fromFrame) || !Number.isFinite(c.toFrame)) continue;
    const from = Math.max(0, Math.min(Math.round(c.fromFrame), srcLen));
    const to = Math.max(0, Math.min(Math.round(c.toFrame), srcLen));
    if (to > from) clamped.push({ fromFrame: from, toFrame: to });
  }
  if (clamped.length === 0) return [];
  clamped.sort((a, b) => a.fromFrame - b.fromFrame);
  const out: VideoCut[] = [{ ...clamped[0] }];
  for (let i = 1; i < clamped.length; i++) {
    const prev = out[out.length - 1];
    const cur = clamped[i];
    // <= (not <) merges touching cuts too, not just overlapping ones.
    if (cur.fromFrame <= prev.toFrame) {
      prev.toFrame = Math.max(prev.toFrame, cur.toFrame);
    } else {
      out.push({ ...cur });
    }
  }
  return out;
}

export function totalCutFrames(cuts: readonly VideoCut[]): number {
  let n = 0;
  for (const c of cuts) n += c.toFrame - c.fromFrame;
  return n;
}

/**
 * SOURCE frame -> OUTPUT frame. Monotone non-decreasing. A frame that falls
 * strictly inside a hole collapses onto that hole's own splice point (the
 * first output frame after the cut) — this isn't a special case, it falls
 * straight out of the subtraction below.
 *
 * `cuts` MUST already be normalized (sorted, merged) — every call site in
 * this module and its callers goes through `normalizeCuts` first.
 */
export function sourceToOutput(f: number, cuts: readonly VideoCut[]): number {
  let removed = 0;
  for (const c of cuts) {
    if (c.fromFrame >= f) break; // sorted: nothing further can overlap [0, f)
    removed += Math.min(c.toFrame, f) - c.fromFrame; // clips a partial overlap when f is inside c
  }
  return f - removed;
}

/**
 * OUTPUT frame -> SOURCE frame. Exact right-inverse of sourceToOutput:
 * sourceToOutput(outputToSource(o, cuts), cuts) === o for every valid o.
 * At a splice point this returns the first KEPT source frame — the frame
 * actually visible there — which is the correct behavior for a seek/add.
 */
export function outputToSource(o: number, cuts: readonly VideoCut[]): number {
  let removed = 0;
  for (const c of cuts) {
    const spliceOut = c.fromFrame - removed;
    if (o < spliceOut) return o + removed;
    removed += c.toFrame - c.fromFrame;
  }
  return o + removed;
}

export type KeptSegment = { srcFrom: number; srcTo: number; outFrom: number };

/** Kept segments in source coordinates, plus each one's OUTPUT start frame — drives <CutVideo>. Every segment satisfies srcTo > srcFrom (never zero-length). */
export function keptSegments(cuts: readonly VideoCut[], srcLen: number): KeptSegment[] {
  const segs: KeptSegment[] = [];
  let cursor = 0;
  let removed = 0;
  for (const c of cuts) {
    if (c.fromFrame > cursor) {
      segs.push({ srcFrom: cursor, srcTo: c.fromFrame, outFrom: cursor - removed });
    }
    removed += c.toFrame - c.fromFrame;
    cursor = c.toFrame;
  }
  if (srcLen > cursor) {
    segs.push({ srcFrom: cursor, srcTo: srcLen, outFrom: cursor - removed });
  }
  return segs;
}

/** The single source of truth for the composition's OUTPUT duration. Every
 * caller (calculateMetadata, the editor's Player, the editor's timeline)
 * MUST use this — never hand-recompute it (see this file's header comment,
 * this exact drift already happened once). */
export function computeOutputDuration(props: {
  durationSeconds?: unknown;
  videoCuts?: unknown;
}): number {
  const fps = 30;
  const srcLen = Math.max(1, Math.ceil((Number(props.durationSeconds) || 1) * fps));
  const cuts = normalizeCuts(
    Array.isArray(props.videoCuts) ? (props.videoCuts as VideoCut[]) : undefined,
    srcLen
  );
  return Math.max(1, srcLen - totalCutFrames(cuts));
}

const MIN_VISIBLE_FRAMES = 8; // below this a mapped range card is a flash, not content

/**
 * Map a single [from, to?) window through the cuts. `to === undefined`
 * means open-ended (runs to the end of the video) and is NEVER dropped —
 * only ranges with a real end can collapse into a cut and disappear.
 * Returns null when the window should be dropped from the DERIVED
 * (rendered) output entirely — never mutates props, see file header.
 */
export function mapWindow(
  from: number,
  to: number | undefined,
  cuts: readonly VideoCut[]
): { from: number; to?: number } | null {
  const oFrom = sourceToOutput(from, cuts);
  if (to === undefined) return { from: oFrom };
  const oTo = sourceToOutput(to, cuts);
  if (oTo - oFrom < MIN_VISIBLE_FRAMES) return null;
  return { from: oFrom, to: oTo };
}

/** Map a single point-in-time frame. Never dropped by itself — collapsing
 * onto a splice point is a legitimate, correct outcome for e.g. a chapter
 * marker. Collapsed RUNS across several keyframes are deduped separately by
 * `mapKeyframes` below, since `interpolate()` hard-throws on a non-strictly-
 * increasing input range. */
export function mapPoint(frame: number, cuts: readonly VideoCut[]): number {
  return sourceToOutput(frame, cuts);
}

/**
 * Map an array of `{frame: number}` keyframes (scenes, opacityKeyframes,
 * timeline.nodes — anything ultimately fed into Remotion's `interpolate()`)
 * and collapse any runs that land on the same output frame. LAST wins on a
 * collapse — that's the state the composition is actually in on the far
 * side of the splice. Without this, two keyframes straddling a cut can map
 * to the same output frame and `interpolate()` throws
 * ("inputRange must be strictly monotonically increasing").
 *
 * Assumes the input is already sorted ascending by `frame` (true for every
 * real caller — scenes/opacityKeyframes/timeline nodes are all authored in
 * chronological order).
 */
export function mapKeyframes<T extends { frame: number }>(
  keyframes: readonly T[],
  cuts: readonly VideoCut[]
): T[] {
  const out: T[] = [];
  for (const kf of keyframes) {
    const mappedFrame = sourceToOutput(kf.frame, cuts);
    const mapped = { ...kf, frame: mappedFrame };
    if (out.length > 0 && out[out.length - 1].frame === mappedFrame) {
      out[out.length - 1] = mapped; // last wins
    } else {
      out.push(mapped);
    }
  }
  return out;
}

/**
 * Map an offset that's relative to an (already output-mapped) parent
 * position — e.g. `dataCards[].rows[].mountOffset`, relative to the card's
 * own `mountFrame`. A cut landing strictly between the parent's mount and
 * mount+offset shortens the effective offset; one landing entirely before
 * or after it leaves the offset unchanged. This is the correct formula
 * (not just "leave it alone"): `sourceToOutput(parentSrcFrame + offset) -
 * outputParentFrame`, using the SOURCE parent frame (not the already-mapped
 * one) so the cut-overlap is computed against the real source timeline.
 */
export function mapOffset(
  parentSrcFrame: number,
  offset: number,
  outputParentFrame: number,
  cuts: readonly VideoCut[]
): number {
  const mappedAbsolute = sourceToOutput(parentSrcFrame + offset, cuts);
  return Math.max(0, mappedAbsolute - outputParentFrame);
}

// ── The field registry ──────────────────────────────────────────────────
//
// Explicit, not schema-derived at runtime — deliberately. The editor's own
// `state/model.ts` TIME_DESCRIPTORS (schema-array-derived) is real and
// reused for the timeline UI, but it is NOT a complete registry of every
// time-bearing field in render_props: it misses six field families (see
// below), because it only ever looks at a top-level array's OWN
// mountFrame/endFrame/fromFrame/toFrame — never nested objects, never a
// sibling *Offset field, never scalars outside an array. This list is the
// complete one. `contracts/render_props.schema.json`'s shapes are frozen
// (see contracts/README.md) — hand-listing them here is the safer choice
// for something a render-correctness bug can hide behind, matching this
// codebase's own existing convention of explicit lists (fieldHints.ts's
// PRIORITY_FIELDS/LABEL_OVERRIDES) for the cases schema-derivation can't
// safely cover. A test (see test-cuts.mjs) walks the actual schema file and
// asserts every numeric time-shaped leaf is accounted for here, so this
// list can't silently go stale as new card types are added.

/** Array sections shaped `{ mountFrame: required, endFrame?: optional }` — the common card shape. */
const MOUNT_END_SECTIONS = [
  "dataCards", "beforeAfter", "gauges", "countdowns", "calendarEvents",
  "progressBars", "milestoneTracks", "barCharts", "milestoneUnlocks",
  "quotes", "pills", "topicCards", "cornerCards", "testimonials",
  "trustBadges", "stepLists", "comparisons", "rankedLists", "checklists",
  "locationPins", "iconClusters", "prosCons",
] as const;

/** Array sections shaped `{ fromFrame: required, toFrame: required }`. */
const FROM_TO_SECTIONS = ["sections", "zoneHeaders"] as const;

/** Array sections shaped `{ frame: required }` (no duration — point markers). */
const POINT_FRAME_SECTIONS = ["scenes", "opacityKeyframes"] as const;

type AnyRec = Record<string, unknown>;

function mapMountEndItem(item: AnyRec, cuts: readonly VideoCut[]): AnyRec | null {
  const mountFrame = item.mountFrame;
  if (typeof mountFrame !== "number") return item;
  const endFrame = typeof item.endFrame === "number" ? item.endFrame : undefined;
  const mapped = mapWindow(mountFrame, endFrame, cuts);
  if (mapped === null) return null;
  const next: AnyRec = { ...item, mountFrame: mapped.from };
  if (mapped.to !== undefined) next.endFrame = mapped.to;
  else delete next.endFrame;
  return next;
}

/** Map one top-level array field through the cuts, applying the item-shape
 * specific to whichever registry bucket `section` belongs to, plus the few
 * per-field extras (secondRevealFrame, nested timeline.nodes, *Offset
 * fields) that a generic shape-based pass can't know about on its own. */
function mapArraySection(section: string, arr: unknown[], cuts: readonly VideoCut[]): unknown[] {
  if ((MOUNT_END_SECTIONS as readonly string[]).includes(section)) {
    const out: unknown[] = [];
    for (const raw of arr) {
      if (typeof raw !== "object" || raw === null) { out.push(raw); continue; }
      const item = raw as AnyRec;
      const mapped = mapMountEndItem(item, cuts);
      if (mapped === null) continue;

      // beforeAfter's secondRevealFrame: a position field TIME_DESCRIPTORS
      // never sees (it's a sibling of mountFrame, not mountFrame/endFrame
      // themselves).
      if (section === "beforeAfter" && typeof item.secondRevealFrame === "number") {
        mapped.secondRevealFrame = sourceToOutput(item.secondRevealFrame, cuts);
      }
      // dataCards[].rows[].mountOffset: offset relative to this card's own
      // mountFrame — mapOffset needs the ORIGINAL (source) mountFrame, not
      // the just-mapped one.
      if (section === "dataCards" && Array.isArray(item.rows)) {
        const outputMount = (mapped.mountFrame as number);
        mapped.rows = (item.rows as unknown[]).map((rawRow) => {
          if (typeof rawRow !== "object" || rawRow === null) return rawRow;
          const row = rawRow as AnyRec;
          if (typeof row.mountOffset !== "number") return row;
          return { ...row, mountOffset: mapOffset(item.mountFrame as number, row.mountOffset, outputMount, cuts) };
        });
      }
      // stepLists[].steps[].activateOffset — same pattern.
      if (section === "stepLists" && Array.isArray(item.steps)) {
        const outputMount = (mapped.mountFrame as number);
        mapped.steps = (item.steps as unknown[]).map((rawStep) => {
          if (typeof rawStep !== "object" || rawStep === null) return rawStep;
          const step = rawStep as AnyRec;
          if (typeof step.activateOffset !== "number") return step;
          return { ...step, activateOffset: mapOffset(item.mountFrame as number, step.activateOffset, outputMount, cuts) };
        });
      }
      // checklists[].items[].activateOffset — same pattern.
      if (section === "checklists" && Array.isArray(item.items)) {
        const outputMount = (mapped.mountFrame as number);
        mapped.items = (item.items as unknown[]).map((rawChecklistItem) => {
          if (typeof rawChecklistItem !== "object" || rawChecklistItem === null) return rawChecklistItem;
          const ci = rawChecklistItem as AnyRec;
          if (typeof ci.activateOffset !== "number") return ci;
          return { ...ci, activateOffset: mapOffset(item.mountFrame as number, ci.activateOffset, outputMount, cuts) };
        });
      }
      out.push(mapped);
    }
    return out;
  }

  if ((FROM_TO_SECTIONS as readonly string[]).includes(section)) {
    const out: unknown[] = [];
    for (const raw of arr) {
      if (typeof raw !== "object" || raw === null) { out.push(raw); continue; }
      const item = raw as AnyRec;
      if (typeof item.fromFrame !== "number" || typeof item.toFrame !== "number") { out.push(item); continue; }
      const mapped = mapWindow(item.fromFrame, item.toFrame, cuts);
      if (mapped === null) continue;
      const next: AnyRec = { ...item, fromFrame: mapped.from, toFrame: mapped.to };

      // sections[].timeline.nodes[].revealFrame — nested two levels below
      // the array item; TIME_DESCRIPTORS only ever looks at the item's own
      // top-level fields, never inside a nested object.
      if (section === "sections" && item.timeline && typeof item.timeline === "object") {
        const timeline = item.timeline as AnyRec;
        if (Array.isArray(timeline.nodes)) {
          next.timeline = {
            ...timeline,
            nodes: mapKeyframes(
              (timeline.nodes as AnyRec[]).map((n) => ({ ...n, frame: n.revealFrame as number })),
              cuts
            ).map((n) => {
              const { frame, ...rest } = n as AnyRec & { frame: number };
              return { ...rest, revealFrame: frame };
            }),
          };
        }
      }
      out.push(next);
    }
    return out;
  }

  if ((POINT_FRAME_SECTIONS as readonly string[]).includes(section)) {
    return mapKeyframes(arr as { frame: number }[], cuts);
  }

  return arr;
}

/**
 * Map an entire render_props object through `cuts` — the composition's own
 * derivation step. Never mutates `props`; with an empty/absent `videoCuts`
 * this is a strict, reference-preserving no-op (returns `props` itself
 * unchanged) so every one of the 73 existing jobs with no cuts renders
 * byte-identical to before this feature existed.
 */
export function mapPropsForCuts<T extends AnyRec>(props: T, cutsInput: readonly VideoCut[]): T {
  if (!cutsInput || cutsInput.length === 0) return props;
  const srcLen = Math.max(1, Math.ceil((Number(props.durationSeconds) || 1) * 30));
  const cuts = normalizeCuts(cutsInput, srcLen);
  if (cuts.length === 0) return props;

  const next: AnyRec = { ...props };

  for (const section of [...MOUNT_END_SECTIONS, ...FROM_TO_SECTIONS, ...POINT_FRAME_SECTIONS]) {
    const arr = (props as AnyRec)[section];
    if (Array.isArray(arr)) next[section] = mapArraySection(section, arr, cuts);
  }

  // captions[]: ms, not frames — convert to frames, map, convert back.
  if (Array.isArray(props.captions)) {
    const fps = 30;
    next.captions = (props.captions as AnyRec[]).map((c) => {
      if (typeof c.startMs !== "number") return c;
      const startFrame = Math.round((c.startMs as number) / 1000 * fps);
      const endFrame = typeof c.endMs === "number" ? Math.round((c.endMs as number) / 1000 * fps) : undefined;
      const mapped = mapWindow(startFrame, endFrame, cuts);
      if (mapped === null) return null;
      const out: AnyRec = { ...c, startMs: Math.round(mapped.from / fps * 1000) };
      if (mapped.to !== undefined) out.endMs = Math.round(mapped.to / fps * 1000);
      return out;
    }).filter((c): c is AnyRec => c !== null);
  }

  // chapters[]: point, atFrame.
  if (Array.isArray(props.chapters)) {
    next.chapters = mapKeyframes(
      (props.chapters as AnyRec[]).map((c) => ({ ...c, frame: c.atFrame as number })),
      cuts
    ).map((c) => {
      const { frame, ...rest } = c as AnyRec & { frame: number };
      return { ...rest, atFrame: frame };
    });
  }

  // Lone scalar position fields, not inside any array.
  if (typeof props.introOutFrame === "number") {
    next.introOutFrame = sourceToOutput(props.introOutFrame as number, cuts);
  }
  if (props.outro && typeof props.outro === "object") {
    const outro = props.outro as AnyRec;
    if (typeof outro.fromFrame === "number") {
      next.outro = { ...outro, fromFrame: sourceToOutput(outro.fromFrame, cuts) };
    }
  }
  if (props.qrContact && typeof props.qrContact === "object") {
    const qrContact = props.qrContact as AnyRec;
    if (typeof qrContact.mountFrame === "number") {
      next.qrContact = { ...qrContact, mountFrame: sourceToOutput(qrContact.mountFrame, cuts) };
    }
  }
  if (props.presenter && typeof props.presenter === "object") {
    const presenter = props.presenter as AnyRec;
    if (Array.isArray(presenter.windows)) {
      const mappedWindows = (presenter.windows as AnyRec[])
        .map((w) => {
          if (typeof w.fromFrame !== "number" || typeof w.toFrame !== "number") return w;
          const mapped = mapWindow(w.fromFrame, w.toFrame, cuts);
          if (mapped === null) return null;
          return { ...w, fromFrame: mapped.from, toFrame: mapped.to };
        })
        .filter((w): w is AnyRec => w !== null);
      next.presenter = { ...presenter, windows: mappedWindows };
    }
  }

  return next as T;
}
