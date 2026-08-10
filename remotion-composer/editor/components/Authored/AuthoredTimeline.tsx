import React, { useCallback, useMemo, useRef, useState } from "react";
import type { ManifestElement } from "../../state/authoredHitTest";
import { frameRef } from "../../state/playhead";
import { sourceToOutput, outputToSource, type VideoCut } from "../../../src/cuts";
import { Clip } from "../Timeline/Clip";
import { Playhead } from "../Timeline/Playhead";
import { Ruler } from "../Timeline/Ruler";
import { useClipDrag } from "../Timeline/useClipDrag";
import { usePinchZoom } from "../Timeline/usePinchZoom";
import { usePlayheadScrub } from "../Timeline/usePlayheadScrub";
import { VideoAudioLanes } from "../Timeline/VideoAudioLanes";
import type { ClipItem } from "../../state/model";
import { chunkWords, type Word } from "../../state/authoredCaptions";

export const CAPTION_ID_PREFIX = "__caption_";

const LABEL_W_DESKTOP = 132;
const LABEL_W_MOBILE = 96;
const ROW_H_DESKTOP = 24;
const ROW_H_MOBILE = 36;

const KIND_LABEL: Record<string, string> = {
  text_block: "Text",
  stat_card: "Stat cards",
  image_swap: "Images",
  broll_window: "B-roll",
};
const KIND_COLOR: Record<string, string> = {
  text_block: "#6C63FF",
  stat_card: "#4FA8E8",
  image_swap: "#E8A84F",
  broll_window: "#4FE8A8",
};
const KIND_ORDER: ManifestElement["kind"][] = ["stat_card", "text_block", "image_swap", "broll_window"];
// Fallback for a kind outside the 4-entry schema enum — KIND_LABEL/KIND_COLOR
// are bare Record lookups, and an unrecognized kind must degrade to
// something renderable rather than `undefined` propagating into a DOM
// style/text prop (confirmed latent risk: nothing in the schema allows this
// today, but authoredValidation.ts's kindValidators has the identical
// bare-Record shape and WOULD throw on an unknown kind — this fallback is
// the cheap half of closing that gap on the render side).
function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}
function kindColor(kind: string): string {
  return KIND_COLOR[kind] ?? "var(--lane-other)";
}

/** Every draggable clip on this timeline is tagged with which system owns
 *  its commit path — "manifest" (Phase 8's own elements, `section` IS the
 *  manifest id) or "caption" (a word-chunk, `section` is a synthetic
 *  `__caption_<key>` id, `captionKey` the real numeric identity — see
 *  state/authoredCaptions.ts's own doc on why chunk array position isn't
 *  stable). `handleTimeEdit` below branches on this field rather than
 *  assuming `clip.section` is always a manifest id — a confirmed design
 *  hazard from the original single-kind version of this file, which
 *  repurposed `ClipItem.section` to carry the manifest id directly with no
 *  discriminator at all. */
type AuthoredClipItem =
  | (ClipItem & { owner: "manifest" })
  | (ClipItem & { owner: "caption"; captionKey: number });

type Lane = { kind: ManifestElement["kind"]; items: AuthoredClipItem[]; rows: number };
type LayerRow = { layer: number; items: AuthoredClipItem[] };

const LABEL_KEYS = ["headline", "text", "label"];
function labelFor(el: ManifestElement): string {
  for (const k of LABEL_KEYS) {
    const v = el[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return el.id;
}

/**
 * Arm B's timeline. Was a thin per-kind-lane view with no Video/Audio
 * strip, no zoom, no Layers view, and no touch gating — this rewrite brings
 * it to parity with Arm A's Timeline.tsx, including cuts: lanes are keyed
 * by manifest `kind` instead of render_props.schema.json's section
 * vocabulary, but otherwise mirrors Arm A's Timeline.tsx/state/model.ts
 * exactly on the one thing that matters for cuts correctness — every
 * element's stored mountFrame/endFrame (and every caption chunk's
 * fromSec/toSec) is SOURCE-frame space (matches what AuthoredScene's own
 * `props.sourceFrame` compares against), while `durationInFrames` here and
 * every on-screen clip position is OUTPUT-frame space (the Player's own
 * coordinate system). `sourceToOutput`/`outputToSource` convert at the two
 * boundaries: reading manifest/caption data in (this file's own memos
 * below) and writing a drag edit back out (handleTimeEdit). With `cuts`
 * empty (the default until Phase 4's Cuts panel is used), both conversions
 * are the identity function — zero behavior change for every job that
 * hasn't made a cut yet.
 */
export function AuthoredTimeline({
  manifest,
  overrides,
  durationInFrames,
  cuts = [],
  selectedId,
  onSelect,
  onCommit,
  onCaptionRetime,
  onSeek,
  hasItemError,
  isTouch,
  filmstripUrls,
  waveformUrl,
  broll,
  words,
  fps,
  className,
  onScrubStart,
  onScrubEnd,
  viewMode: viewModeProp,
  onViewModeChange,
}: {
  manifest: ManifestElement[];
  overrides: Record<string, Record<string, unknown>>;
  /** OUTPUT-frame duration — see this component's own doc comment above. */
  durationInFrames: number;
  /** Normalized cuts (SOURCE-video frames) — same shape/convention as Arm
   *  A's Timeline.tsx `cuts` prop. Defaults to empty (no cuts yet). */
  cuts?: VideoCut[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onCommit: (id: string, patch: { mountFrame?: number; endFrame?: number }) => void;
  /** Caption chunk retime (drag/trim) — frames, in the SAME timeline-frame
   *  space as everything else here; AuthoredEditor.tsx converts to seconds
   *  and applies the actual word-level remap (see authoredCaptions.ts). */
  onCaptionRetime?: (chunkKey: number, fromFrame: number, toFrame: number) => void;
  onSeek: (frame: number) => void;
  hasItemError?: (id: string) => boolean;
  isTouch: boolean;
  filmstripUrls?: string[];
  waveformUrl?: string | null;
  /** Reference-only b-roll windows straight from the job's props.broll —
   *  independent of any broll_window manifest entry, and always empty on
   *  every real job seen so far, hence rendered only when non-empty rather
   *  than as a permanent lane that's always blank. */
  broll?: { src: string; label: string; startFrame: number; endFrame: number }[];
  /** Effective (override-applied) words — the same array AuthoredEditor.tsx
   *  feeds the live preview's inputProps.words, so the Captions lane always
   *  shows exactly what's currently playing, edited or not. */
  words?: Word[];
  fps: number;
  className?: string;
  /** Pause/resume playback around a playhead scrub — see usePlayheadScrub.ts. */
  onScrubStart?: () => void;
  onScrubEnd?: () => void;
  /** Controlled view mode — mirrors Arm A's Timeline.tsx: AuthoredPhoneShell's
   *  action-bar Layers button needs to read/drive this from outside. Omit
   *  both to keep the default self-contained behavior (internal state, the
   *  in-bar "By type"/"Layers" toggle buttons only) — the desktop
   *  AuthoredEditor.tsx call site does exactly that. */
  viewMode?: "type" | "layers";
  onViewModeChange?: (mode: "type" | "layers") => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const hudRef = useRef<HTMLDivElement>(null);
  const [pxPerFrame, setPxPerFrame] = useState(0);
  const [internalViewMode, setInternalViewMode] = useState<"type" | "layers">("type");
  const viewMode = viewModeProp ?? internalViewMode;
  const setViewMode = onViewModeChange ?? setInternalViewMode;

  const labelWidth = isTouch ? LABEL_W_MOBILE : LABEL_W_DESKTOP;
  const rowHeight = isTouch ? ROW_H_MOBILE : ROW_H_DESKTOP;

  React.useEffect(() => {
    if (pxPerFrame > 0) return;
    const el = scrollRef.current;
    if (!el || durationInFrames <= 0) return;
    const avail = el.clientWidth - labelWidth - 16;
    if (avail > 0) setPxPerFrame(avail / durationInFrames);
  }, [pxPerFrame, durationInFrames, labelWidth]);

  const zoomFit = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const avail = el.clientWidth - labelWidth - 16;
    if (avail > 0) setPxPerFrame(avail / durationInFrames);
    el.scrollLeft = 0;
  }, [durationInFrames, labelWidth]);

  const effectivePx = pxPerFrame || 1;
  const canvasWidth = labelWidth + durationInFrames * effectivePx;

  // Element from/to are stored SOURCE-frame (matches the wrapper's own
  // sourceFrame prop, see this file's doc comment) — sourceToOutput maps
  // each to this timeline's OUTPUT coordinate system before layout; a no-op
  // while `cuts` is empty. Open-ended clips use `durationInFrames` (already
  // OUTPUT) directly, not converted again.
  const elementRange = useCallback((el: ManifestElement, o: Record<string, unknown>) => {
    const fromSrc = typeof o.mountFrame === "number" ? o.mountFrame : (el.mountFrame ?? 0);
    const from = sourceToOutput(fromSrc, cuts);
    const openEnded = el.endFrame == null && o.endFrame == null;
    if (openEnded) return { from, to: durationInFrames, openEnded: true };
    const toSrc = typeof o.endFrame === "number" ? o.endFrame : (el.endFrame as number);
    const to = Math.max(sourceToOutput(toSrc, cuts), from + 1);
    return { from, to, openEnded: false };
  }, [cuts, durationInFrames]);

  // Effective (override-applied) from/to per element, plus a simple greedy
  // row-packer within each kind's lane so overlapping windows don't paint
  // on top of each other — Arm A's buildLanes() does the same job but is
  // tied to render_props.schema.json's own section vocabulary, not reusable
  // here.
  const lanes: Lane[] = useMemo(() => {
    const byKind = new Map<string, ManifestElement[]>();
    for (const el of manifest) {
      if (!byKind.has(el.kind)) byKind.set(el.kind, []);
      byKind.get(el.kind)!.push(el);
    }
    const knownKinds = new Set<string>(KIND_ORDER);
    const orderedKinds: string[] = [...KIND_ORDER, ...Array.from(byKind.keys()).filter((k) => !knownKinds.has(k))];
    const out: Lane[] = [];
    for (const kind of orderedKinds) {
      const els = byKind.get(kind);
      if (!els || els.length === 0) continue;
      const sorted = [...els].sort((a, b) => (a.mountFrame ?? 0) - (b.mountFrame ?? 0));
      const rowEnds: number[] = [];
      const items: AuthoredClipItem[] = sorted.map((el) => {
        const o = overrides[el.id] || {};
        const { from, to, openEnded } = elementRange(el, o);
        let row = rowEnds.findIndex((end) => end <= from);
        if (row === -1) { row = rowEnds.length; rowEnds.push(to); } else { rowEnds[row] = to; }
        return { section: el.id, index: 0, from, to, openEnded, isPoint: false, label: labelFor(el), row, owner: "manifest" };
      });
      out.push({ kind: kind as ManifestElement["kind"], items, rows: Math.max(1, rowEnds.length) });
    }
    return out;
  }, [manifest, overrides, elementRange]);

  // Layers view (read-only — see the module-level comment on why: 0 of 18
  // real generated scenes read `layer` from overrides, so a drag here would
  // change neither the render nor authoredHitTest's selection priority).
  // Ascending by layer number, non-empty rows only — mirrors state/layers.ts's
  // ordering convention without its "+Add row" scratch-row affordance, which
  // has nothing to attach to when dragging is disabled.
  const layerRows: LayerRow[] = useMemo(() => {
    const byLayer = new Map<number, AuthoredClipItem[]>();
    for (const el of manifest) {
      const o = overrides[el.id] || {};
      const { from, to, openEnded } = elementRange(el, o);
      const layer = typeof o.layer === "number" ? o.layer : el.layer;
      const item: AuthoredClipItem = { section: el.id, index: 0, from, to, openEnded, isPoint: false, label: labelFor(el), row: 0, owner: "manifest" };
      if (!byLayer.has(layer)) byLayer.set(layer, []);
      byLayer.get(layer)!.push(item);
    }
    return Array.from(byLayer.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([layer, items]) => ({ layer, items: items.sort((a, b) => a.from - b.from) }));
  }, [manifest, overrides, elementRange]);

  // Captions lane — chunkWords() always produces non-overlapping, sequential
  // chunks by construction, so unlike the manifest lanes this never needs
  // row-packing (every chunk fits in row 0). fromSec/toSec are SOURCE-time
  // (word timestamps, transcript-derived) — convert to OUTPUT frames the
  // same way element ranges are, above.
  const captionItems: AuthoredClipItem[] = useMemo(() => {
    const chunks = chunkWords(words || []);
    return chunks.map((c) => {
      const from = sourceToOutput(Math.round(c.fromSec * fps), cuts);
      const to = Math.max(sourceToOutput(Math.round(c.toSec * fps), cuts), from + 1);
      return {
        section: `${CAPTION_ID_PREFIX}${c.key}`,
        index: 0,
        from,
        to,
        openEnded: false,
        isPoint: false,
        label: c.text || "(empty)",
        row: 0,
        owner: "caption" as const,
        captionKey: c.key,
      };
    });
  }, [words, fps, cuts]);

  const getSnapTargets = useCallback((clip: ClipItem): number[] => {
    if ((clip as AuthoredClipItem).owner === "caption") {
      const siblings: number[] = [];
      for (const it of captionItems) {
        if (it.section === clip.section) continue;
        siblings.push(it.from, it.to);
      }
      return [frameRef.current, ...siblings, 0, durationInFrames];
    }
    const lane = lanes.find((l) => l.items.some((it) => it.section === clip.section));
    const siblings: number[] = [];
    if (lane) {
      for (const it of lane.items) {
        if (it.section === clip.section) continue;
        siblings.push(it.from, it.to);
      }
    }
    return [frameRef.current, ...siblings, 0, durationInFrames];
  }, [lanes, captionItems, durationInFrames]);

  // edit.from/edit.to arrive in OUTPUT frames (the drag/UI layer's own
  // coordinate system, same as every clip position built above) — clamp in
  // OUTPUT space against this timeline's own bounds, then convert to SOURCE
  // only at the point of writing (mountFrame/endFrame/caption seconds all
  // live on disk in SOURCE space). Identity while cuts is empty.
  const toSourceFrame = useCallback(
    (outputFrame: number) => outputToSource(Math.max(0, Math.min(Math.round(outputFrame), durationInFrames)), cuts),
    [cuts, durationInFrames]
  );

  // Owner-routed commit — see AuthoredClipItem's own doc comment above for
  // why this branches instead of assuming `clip.section` is always a
  // manifest id.
  const handleTimeEdit = useCallback((clip: ClipItem, edit: { from?: number; to?: number }) => {
    const authored = clip as AuthoredClipItem;
    if (authored.owner === "manifest") {
      const patch: { mountFrame?: number; endFrame?: number } = {};
      if (edit.from != null) patch.mountFrame = toSourceFrame(edit.from);
      if (edit.to != null) patch.endFrame = toSourceFrame(edit.to);
      onCommit(clip.section, patch);
    } else if (authored.owner === "caption") {
      const from = toSourceFrame(edit.from ?? clip.from);
      const to = toSourceFrame(edit.to ?? clip.to);
      onCaptionRetime?.(authored.captionKey, from, to);
    }
  }, [onCommit, onCaptionRetime, toSourceFrame]);

  const handleSelectClip = useCallback((clip: ClipItem) => {
    onSelect(clip.section);
    onSeek(clip.from);
  }, [onSelect, onSeek]);

  const isSelectedClip = useCallback((id: string) => selectedId === id, [selectedId]);

  const { onPointerDown, cancelDrag: cancelClipDrag, dragHandlers } = useClipDrag({
    pxPerFrame: effectivePx,
    durationInFrames,
    getSnapTargets,
    onCommit: handleTimeEdit,
    onSelect: handleSelectClip,
    hudRef,
    isDragAllowed: (clip) => !isTouch || isSelectedClip(clip.section),
  });

  // Layers view is read-only (see layerRows' own comment) — reuses Clip.tsx
  // for visual consistency with the "type" view, but its onPointerDown only
  // selects/seeks, never starts a drag, and its move/up/cancel handlers are
  // no-ops. `resizable`/`dragEnabled` both false so Clip never renders
  // resize handles for these.
  const readOnlyOnPointerDown = useCallback((clip: ClipItem) => (e: React.PointerEvent<HTMLElement>) => {
    handleSelectClip(clip);
  }, [handleSelectClip]);
  const readOnlyDragHandlers = useMemo(() => ({
    onPointerMove: () => {},
    onPointerUp: () => {},
    onPointerCancel: () => {},
  }), []);

  const pxPerFrameRef = useRef(effectivePx);
  pxPerFrameRef.current = effectivePx;

  // Splice markers: each cut collapses to one OUTPUT-frame point — same
  // mechanism/comment as Arm A's Timeline.tsx.
  const spliceFrames = useMemo(
    () => cuts.map((c) => sourceToOutput(c.fromFrame, cuts)),
    [cuts]
  );

  const { scrubHandlers, cancelScrub } = usePlayheadScrub({
    scrollRef,
    labelWidth,
    pxPerFrameRef,
    durationInFrames,
    onSeek,
    onScrubStart,
    onScrubEnd,
  });

  const { pinchHandlers } = usePinchZoom({
    scrollRef,
    labelWidth,
    pxPerFrameRef,
    setPxPerFrame,
    cancelActiveDrags: useCallback(() => {
      cancelClipDrag();
      cancelScrub();
    }, [cancelClipDrag, cancelScrub]),
  });

  const totalRows = viewMode === "layers"
    ? layerRows.length
    : lanes.reduce((h, l) => h + l.rows, 0);
  const brollRows = broll && broll.length > 0 ? 1 : 0;
  const captionRows = captionItems.length > 0 ? 1 : 0;
  const totalHeight = (totalRows + brollRows + captionRows) * rowHeight + rowHeight * 3;

  return (
    <div className={`timeline app__timeline${className ? ` ${className}` : ""}`}>
      <div className="timeline__bar">
        <span className="toolbar__meta">Timeline</span>
        <div className="timeline__viewtoggle">
          <button type="button" className="btn btn--sm" aria-selected={viewMode === "type"} onClick={() => setViewMode("type")} title="Group clips by element type">By type</button>
          <button type="button" className="btn btn--sm" aria-selected={viewMode === "layers"} onClick={() => setViewMode("layers")} title="Group by stacking order (read-only — this scene's generated code doesn't wire layer edits to the render yet)">Layers</button>
        </div>
        <span className="toolbar__spacer" />
        <button type="button" className="btn btn--sm" onClick={() => setPxPerFrame((z) => Math.max(0.05, (z || 1) / 1.6))} title="Zoom out">−</button>
        <button type="button" className="btn btn--sm" onClick={zoomFit} title="Fit to width">Fit</button>
        <button type="button" className="btn btn--sm" onClick={() => setPxPerFrame((z) => Math.min(40, (z || 1) * 1.6))} title="Zoom in">+</button>
      </div>

      <div className="timeline__scroll" ref={scrollRef} {...pinchHandlers}>
        <div className="timeline__canvas" style={{ width: canvasWidth }}>
          <div className="timeline__ruler-wrap" {...scrubHandlers}>
            <Ruler durationInFrames={durationInFrames} pxPerFrame={effectivePx} labelWidth={labelWidth} />
          </div>

          {lanes.length === 0 && (
            <div className="inspector__empty">
              Nothing to show — this scene's manifest has no editable elements.
            </div>
          )}

          <VideoAudioLanes
            durationInFrames={durationInFrames}
            pxPerFrame={effectivePx}
            labelWidth={labelWidth}
            rowHeight={rowHeight}
            filmstripUrls={filmstripUrls}
            waveformUrl={waveformUrl}
            spliceFrames={spliceFrames}
            spliceTitle={(i) => `Cut here (${(cuts[i]?.toFrame ?? 0) - (cuts[i]?.fromFrame ?? 0)} frames removed)`}
          />

          {captionItems.length > 0 && (
            <div className="lane" style={{ height: rowHeight }}>
              <div className="lane__label">
                <span className="lane__swatch" style={{ background: "#FF6EC7" }} />
                <span title="Chunk boundaries here are an estimate — the render may group these words slightly differently. Word text and timing are exact.">
                  Captions (approx. grouping)
                </span>
                <span className="lane__count">{captionItems.length}</span>
              </div>
              <div
                className="lane__track"
                style={{ left: labelWidth }}
                {...scrubHandlers}
                onPointerDown={(e) => {
                  if (e.target !== e.currentTarget) return;
                  onSelect(null);
                  scrubHandlers.onPointerDown(e);
                }}
              >
                {captionItems.map((clip) => (
                  <Clip
                    key={clip.section}
                    clip={clip}
                    pxPerFrame={effectivePx}
                    rowHeight={rowHeight}
                    selected={selectedId === clip.section}
                    invalid={false}
                    revealed={false}
                    color="#FF6EC7"
                    resizable
                    dragEnabled={!isTouch || selectedId === clip.section}
                    onPointerDown={onPointerDown}
                    dragHandlers={dragHandlers}
                  />
                ))}
              </div>
            </div>
          )}

          {broll && broll.length > 0 && (
            <div className="lane" style={{ height: rowHeight }}>
              <div className="lane__label">
                <span className="lane__swatch" style={{ background: "#4FE8A8" }} />
                <span>B-roll (reference)</span>
                <span className="lane__count">{broll.length}</span>
              </div>
              <div className="lane__track" style={{ left: labelWidth }}>
                {broll.map((b, i) => (
                  <div
                    key={i}
                    className="clip"
                    style={{
                      left: b.startFrame * effectivePx,
                      width: Math.max(3, (b.endFrame - b.startFrame) * effectivePx),
                      top: 2,
                      height: rowHeight - 5,
                      background: "#4FE8A8",
                      opacity: 0.6,
                    }}
                    title={`${b.label || "b-roll"} — frames ${b.startFrame}–${b.endFrame} (reference only, not editable here)`}
                  >
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{b.label || "b-roll"}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {viewMode === "type" && lanes.map((lane) => (
            <div className="lane" key={lane.kind} style={{ height: lane.rows * rowHeight }}>
              <div className="lane__label">
                <span className="lane__swatch" style={{ background: kindColor(lane.kind) }} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {kindLabel(lane.kind)}
                </span>
                <span className="lane__count">{lane.items.length}</span>
              </div>
              <div
                className="lane__track"
                style={{ left: labelWidth }}
                {...scrubHandlers}
                onPointerDown={(e) => {
                  if (e.target !== e.currentTarget) return;
                  onSelect(null);
                  scrubHandlers.onPointerDown(e);
                }}
              >
                {lane.items.map((clip) => (
                  <Clip
                    key={clip.section}
                    clip={clip}
                    pxPerFrame={effectivePx}
                    rowHeight={rowHeight}
                    selected={selectedId === clip.section}
                    invalid={hasItemError?.(clip.section) ?? false}
                    revealed={false}
                    color={kindColor(lane.kind)}
                    resizable
                    dragEnabled={!isTouch || selectedId === clip.section}
                    onPointerDown={onPointerDown}
                    dragHandlers={dragHandlers}
                  />
                ))}
              </div>
            </div>
          ))}

          {viewMode === "layers" && layerRows.map((row) => (
            <div className="lane" key={row.layer} style={{ height: rowHeight }}>
              <div className="lane__label">
                <span className="lane__swatch" style={{ background: "var(--lane-other)" }} />
                <span>Layer {row.layer}</span>
                <span className="lane__count">{row.items.length}</span>
              </div>
              <div
                className="lane__track"
                style={{ left: labelWidth }}
                {...scrubHandlers}
                onPointerDown={(e) => {
                  if (e.target !== e.currentTarget) return;
                  onSelect(null);
                  scrubHandlers.onPointerDown(e);
                }}
              >
                {row.items.map((clip) => (
                  <Clip
                    key={clip.section}
                    clip={clip}
                    pxPerFrame={effectivePx}
                    rowHeight={rowHeight}
                    selected={selectedId === clip.section}
                    invalid={hasItemError?.(clip.section) ?? false}
                    revealed={false}
                    color="var(--accent)"
                    resizable={false}
                    dragEnabled={false}
                    onPointerDown={readOnlyOnPointerDown}
                    dragHandlers={readOnlyDragHandlers}
                  />
                ))}
              </div>
            </div>
          ))}

          <Playhead pxPerFrame={effectivePx} labelWidth={labelWidth} height={totalHeight + 26} scrubHandlers={scrubHandlers} />
          <div className="draghud" ref={hudRef} style={{ display: "none" }} />
        </div>
      </div>
    </div>
  );
}
