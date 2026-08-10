import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ClipItem, Lane } from "../../state/model";
import { canResize } from "../../state/model";
import type { LayerClipItem, LayerRow } from "../../state/layers";
import { FPS, frameRef } from "../../state/playhead";
import { sourceToOutput, type VideoCut } from "../../../src/cuts";
import { Clip } from "./Clip";
import { LayerClip } from "./LayerClip";
import { Playhead } from "./Playhead";
import { Ruler } from "./Ruler";
import { useClipDrag } from "./useClipDrag";
import { useLayerDrag } from "./useLayerDrag";
import { usePinchZoom } from "./usePinchZoom";
import { usePlayheadScrub } from "./usePlayheadScrub";
import { VideoAudioLanes } from "./VideoAudioLanes";

// index: null selects a top-level OBJECT field — Timeline itself never
// PRODUCES one (every lane item comes from a real array), but must accept
// one being passed in as the current selection (nothing here will match it,
// which is correct: none of this timeline's clips correspond to it).
export type Selection = { section: string; index: number | null } | null;

const LABEL_W_DESKTOP = 132;
const LABEL_W_MOBILE = 96;
const ROW_H_DESKTOP = 24;
const ROW_H_MOBILE = 36;

export type TimelineViewMode = "type" | "layers";

export function Timeline({
  lanes,
  layerRows,
  durationInFrames,
  selection,
  onSelect,
  onSeek,
  onTimeEdit,
  onLayerEdit,
  hasItemError,
  isTouch,
  className,
  filmstripUrls,
  waveformUrl,
  cuts,
  viewMode: viewModeProp,
  onViewModeChange,
  onScrubStart,
  onScrubEnd,
}: {
  lanes: Lane[];
  /** Alternative grouping for the "Layers" view (by stacking order instead of by section) — see state/layers.ts. */
  layerRows: LayerRow[];
  durationInFrames: number;
  selection: Selection;
  onSelect: (sel: Selection) => void;
  onSeek: (frame: number) => void;
  onTimeEdit: (clip: ClipItem, edit: { from?: number; to?: number }) => void;
  onLayerEdit: (clip: LayerClipItem, newLayer: number) => void;
  hasItemError: (section: string, index: number) => boolean;
  isTouch: boolean;
  className?: string;
  /** Raw-source thumbnails/waveform for the Video/Audio synthetic lanes. Empty/null renders the lane with just its label, no image content yet. */
  filmstripUrls?: string[];
  waveformUrl?: string | null;
  /** Normalized cuts (source-video frames) — drawn as splice markers on the Video/Audio lanes. Cut creation itself lives in the Inspector (touch-friendly buttons), not timeline drag — see components/Inspector/ProjectInspector.tsx. */
  cuts?: VideoCut[];
  /** Controlled view mode — the phone shell's Layers action-bar button needs
   *  to read/drive this from outside (Phase 6, F2). Omit both to keep
   *  today's fully self-contained desktop behavior (internal state, the
   *  in-bar "By type"/"Layers" toggle buttons only). */
  viewMode?: TimelineViewMode;
  onViewModeChange?: (mode: TimelineViewMode) => void;
  /** Pause/resume playback around a playhead scrub — seekTo alone doesn't
   *  pause, so without this the video keeps advancing under a drag. Omit
   *  both to leave playback untouched. */
  onScrubStart?: () => void;
  onScrubEnd?: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const hudRef = useRef<HTMLDivElement>(null);
  const layerHudRef = useRef<HTMLDivElement>(null);
  const [pxPerFrame, setPxPerFrame] = useState(0);
  const [internalViewMode, setInternalViewMode] = useState<TimelineViewMode>("type");
  const viewMode = viewModeProp ?? internalViewMode;
  const setViewMode = onViewModeChange ?? setInternalViewMode;
  const [extraRows, setExtraRows] = useState(0);

  const labelWidth = isTouch ? LABEL_W_MOBILE : LABEL_W_DESKTOP;
  const rowHeight = isTouch ? ROW_H_MOBILE : ROW_H_DESKTOP;

  // 初始缩放 = 铺满可视宽度。等测到真实宽度再设，避免第一帧闪一个错误的比例。
  useEffect(() => {
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

  // "+ Add row" is purely a UI affordance over an integer (the item's own
  // `layer` field) — an empty row has nothing to persist, so it's tracked
  // as local state here and simply stops rendering on reload, rather than
  // inventing a `layers: []` array just to remember that emptiness existed.
  const displayLayerRows = useMemo(() => {
    if (extraRows <= 0) return layerRows;
    const highest = layerRows.length > 0 ? layerRows[layerRows.length - 1].layer : -1;
    const extra = Array.from({ length: extraRows }, (_, i) => ({ layer: highest + 1 + i, items: [] }));
    return [...layerRows, ...extra];
  }, [layerRows, extraRows]);

  // +3 rows for the Video (2 rows tall) + Audio (1 row) synthetic lanes
  // pinned above the schema-driven ones (present in both view modes).
  const bodyRows = viewMode === "layers" ? displayLayerRows.length : lanes.reduce((h, l) => h + l.rows, 0);
  const totalHeight = bodyRows * rowHeight + rowHeight * 3;

  // 吸附目标：播放头 > 同轨其它 clip 边缘 > 章节点 > 整秒 > 首尾。
  const chapterFrames = useMemo(() => {
    const ch = lanes.find((l) => l.section === "chapters");
    return ch ? ch.items.map((i) => i.from) : [];
  }, [lanes]);

  const wholeSeconds = useMemo(() => {
    const out: number[] = [];
    for (let s = 0; s * FPS <= durationInFrames; s++) out.push(s * FPS);
    return out;
  }, [durationInFrames]);

  const getSnapTargets = useCallback((clip: ClipItem): number[] => {
    const lane = lanes.find((l) => l.section === clip.section);
    const siblings: number[] = [];
    if (lane) {
      for (const it of lane.items) {
        if (it.index === clip.index) continue;
        siblings.push(it.from, it.to);
      }
    }
    return [frameRef.current, ...siblings, ...chapterFrames, ...wholeSeconds, 0, durationInFrames];
  }, [lanes, chapterFrames, wholeSeconds, durationInFrames]);

  const handleTimeEdit = useCallback((clip: ClipItem, edit: { from?: number; to?: number }) => {
    onTimeEdit(clip, edit);
  }, [onTimeEdit]);

  const handleSelectClip = useCallback((clip: ClipItem) => {
    onSelect({ section: clip.section, index: clip.index });
    onSeek(clip.from);
  }, [onSelect, onSeek]);

  const isSelectedClip = useCallback(
    (section: string, index: number) =>
      !!selection && selection.section === section && selection.index === index,
    [selection]
  );

  // 触屏上默认不开拖拽：33 秒铺在约 350px 上，一个 clip 只有几 px，拖不准，
  // 而且会跟页面滚动打架。但已经选中的 clip 是例外——用户已经明确"就是它"，
  // 这时手指划它就是要挪它，不是要滚动时间轴（Phase 6, F3a）。
  const { onPointerDown, cancelDrag: cancelClipDrag, dragHandlers } = useClipDrag({
    pxPerFrame: effectivePx,
    durationInFrames,
    getSnapTargets,
    onCommit: handleTimeEdit,
    onSelect: handleSelectClip,
    hudRef,
    isDragAllowed: (clip) => !isTouch || isSelectedClip(clip.section, clip.index),
  });

  const handleSelectLayerClip = useCallback((clip: LayerClipItem) => {
    onSelect({ section: clip.section, index: clip.index });
    onSeek(clip.from);
  }, [onSelect, onSeek]);

  const handleLayerEdit = useCallback((clip: LayerClipItem, newLayer: number) => {
    onLayerEdit(clip, newLayer);
  }, [onLayerEdit]);

  const maxLayerRow = Math.max(0, displayLayerRows.length - 1);
  const { onPointerDown: onLayerPointerDown, cancelDrag: cancelLayerDrag, dragHandlers: layerDragHandlers } = useLayerDrag({
    rowHeight,
    maxLayer: maxLayerRow,
    onCommit: handleLayerEdit,
    onSelect: handleSelectLayerClip,
    hudRef: layerHudRef,
    isDragAllowed: (clip) => !isTouch || isSelectedClip(clip.section, clip.index),
  });

  // Kept in sync every render (cheap: one write) rather than read from the
  // `pxPerFrame` state directly — usePinchZoom's rAF-throttled callback can
  // fire after several re-renders already happened this gesture, and a
  // stale closure over the state value would compound the wrong base zoom
  // into each successive frame of the same pinch.
  const pxPerFrameRef = useRef(effectivePx);
  pxPerFrameRef.current = effectivePx;

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
      cancelLayerDrag();
      cancelScrub();
    }, [cancelClipDrag, cancelLayerDrag, cancelScrub]),
  });

  // Splice markers: each cut collapses to one OUTPUT-frame point (its start
  // and end map to the same output frame — see src/cuts.ts's sourceToOutput)
  // — drawn on the Video/Audio lanes so a cut is visible without needing to
  // open the Inspector's cuts list.
  const spliceFrames = useMemo(
    () => (cuts || []).map((c) => sourceToOutput(c.fromFrame, cuts || [])),
    [cuts]
  );

  // Reveal + flash the clip matching an externally-driven selection (Phase
  // 6, F1d — clicking a card on the video preview). Keyed on a joined
  // string, not the `selection` object itself: App.tsx's setSelection
  // produces a fresh object on every call, but this effect must only fire
  // when the SELECTED ITEM actually changes, not on every unrelated
  // re-render that happens to pass an object with the same content.
  const selKey = selection ? `${selection.section}:${selection.index}` : null;
  const [revealedKey, setRevealedKey] = useState<string | null>(null);

  useEffect(() => {
    if (!selKey || !selection) {
      setRevealedKey(null);
      return;
    }
    const scrollEl = scrollRef.current;
    if (!scrollEl) return;

    let targetTop: number | null = null;
    let targetFrom = 0;
    let targetTo = 0;

    if (viewMode === "type") {
      const lane = lanes.find((l) => l.section === selection.section);
      const clip = lane?.items.find((it) => it.index === selection.index);
      if (lane && clip) {
        let yOffset = rowHeight * 3; // Video + Audio synthetic lanes, always on top
        for (const l of lanes) {
          if (l.section === lane.section) break;
          yOffset += l.rows * rowHeight;
        }
        targetTop = yOffset + clip.row * rowHeight;
        targetFrom = clip.from;
        targetTo = clip.isPoint ? clip.from : clip.to;
      }
    } else {
      let yOffset = rowHeight * 3;
      for (const row of displayLayerRows) {
        const clip = row.items.find((it) => it.section === selection.section && it.index === selection.index);
        if (clip) {
          targetTop = yOffset;
          targetFrom = clip.from;
          targetTo = clip.isPoint ? clip.from : clip.to;
          break;
        }
        yOffset += rowHeight;
      }
    }

    // Selection isn't part of the current view at all (e.g. it's a section
    // the hand-maintained Layers model in state/layers.ts doesn't know
    // about yet) — nothing to scroll to, degrade silently.
    if (targetTop == null) return;

    const clipLeft = labelWidth + targetFrom * effectivePx;
    const clipRight = labelWidth + Math.max(targetTo, targetFrom + 1) * effectivePx;
    const viewLeft = scrollEl.scrollLeft;
    const viewRight = viewLeft + scrollEl.clientWidth;
    const viewTop = scrollEl.scrollTop;
    const viewBottom = viewTop + scrollEl.clientHeight;

    // Only scroll on the axis that's actually out of view — never yank a
    // view that already shows the clip, which would fight the user every
    // time they select something they can already see.
    const needsScrollX = clipLeft < viewLeft || clipRight > viewRight;
    const needsScrollY = targetTop < viewTop || targetTop + rowHeight > viewBottom;

    if (needsScrollX || needsScrollY) {
      const reducedMotion =
        typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      scrollEl.scrollTo({
        left: needsScrollX ? Math.max(0, clipLeft - scrollEl.clientWidth / 2) : viewLeft,
        top: needsScrollY ? Math.max(0, targetTop - scrollEl.clientHeight / 2) : viewTop,
        behavior: reducedMotion ? "auto" : "smooth",
      });
    }

    setRevealedKey(selKey);
    const t = window.setTimeout(() => {
      setRevealedKey((cur) => (cur === selKey ? null : cur));
    }, 600);
    return () => window.clearTimeout(t);
    // Deliberately NOT depending on lanes/displayLayerRows/rowHeight/labelWidth/
    // effectivePx — this must fire once per SELECTION change, not on every
    // render where those recompute (a prop edit elsewhere would otherwise
    // re-trigger a scroll+flash on a selection the user never touched).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selKey, viewMode]);

  return (
    <div className={`timeline app__timeline${className ? ` ${className}` : ""}`}>
      <div className="timeline__bar">
        <span className="toolbar__meta">Timeline</span>
        <div className="timeline__viewtoggle">
          <button type="button" className="btn btn--sm" aria-selected={viewMode === "type"} onClick={() => setViewMode("type")} title="Group clips by card type">By type</button>
          <button type="button" className="btn btn--sm" aria-selected={viewMode === "layers"} onClick={() => setViewMode("layers")} title="Group clips by stacking order — drag a clip up/down to change what draws on top">Layers</button>
        </div>
        {viewMode === "layers" && (
          <button type="button" className="btn btn--sm" onClick={() => setExtraRows((n) => n + 1)} title="Add an empty layer row to drop a clip into">+ Add row</button>
        )}
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
              Nothing on the timeline yet — add a card from the panel on the right.
            </div>
          )}

          {/* Video/Audio synthetic lanes — pinned above the schema-driven
              lanes below. Not part of buildLanes()/TIME_DESCRIPTORS: a
              single full-duration raw-source clip has no schema array to
              derive from (see state/model.ts's own doc comment on why one
              lane = one array field). Read-only reference strips — cuts are
              made via the Inspector's buttons (touch-friendly, and works
              identically on desktop), not by dragging here; the vertical
              splice markers below show where existing cuts land. Shared
              with Arm B via VideoAudioLanes.tsx (extracted so both editors
              render an identical Video/Audio track instead of Arm B simply
              lacking one). */}
          <VideoAudioLanes
            durationInFrames={durationInFrames}
            pxPerFrame={effectivePx}
            labelWidth={labelWidth}
            rowHeight={rowHeight}
            filmstripUrls={filmstripUrls}
            waveformUrl={waveformUrl}
            spliceFrames={spliceFrames}
            spliceTitle={(i) => `Cut here (${(cuts?.[i].toFrame ?? 0) - (cuts?.[i].fromFrame ?? 0)} frames removed)`}
          />

          {viewMode === "type" && lanes.map((lane) => (
            <div className="lane" key={lane.section} style={{ height: lane.rows * rowHeight }}>
              <div className="lane__label">
                <span className="lane__swatch" style={{ background: lane.color }} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {lane.label}
                </span>
                <span className="lane__count">{lane.items.length}</span>
              </div>
              <div
                className="lane__track"
                style={{ left: labelWidth }}
                {...scrubHandlers}
                onPointerDown={(e) => {
                  // 点空白处 = 移动播放头并取消选中（Figma/Premiere 的习惯）。
                  if (e.target !== e.currentTarget) return;
                  onSelect(null);
                  scrubHandlers.onPointerDown(e);
                }}
              >
                {lane.items.map((clip) => {
                  const selected = isSelectedClip(clip.section, clip.index);
                  return (
                    <Clip
                      key={`${clip.section}-${clip.index}`}
                      clip={clip}
                      pxPerFrame={effectivePx}
                      rowHeight={rowHeight}
                      selected={selected}
                      invalid={hasItemError(clip.section, clip.index)}
                      revealed={revealedKey === `${clip.section}:${clip.index}`}
                      color={lane.color}
                      resizable={canResize(clip.section)}
                      dragEnabled={!isTouch || selected}
                      onPointerDown={onPointerDown}
                      dragHandlers={dragHandlers}
                    />
                  );
                })}
              </div>
            </div>
          ))}

          {viewMode === "layers" && displayLayerRows.map((row) => (
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
                {row.items.map((clip) => {
                  const selected = isSelectedClip(clip.section, clip.index);
                  return (
                    <LayerClip
                      key={`${clip.section}-${clip.index}`}
                      clip={clip}
                      pxPerFrame={effectivePx}
                      selected={selected}
                      revealed={revealedKey === `${clip.section}:${clip.index}`}
                      color="var(--accent)"
                      dragEnabled={!isTouch || selected}
                      onPointerDown={onLayerPointerDown}
                      dragHandlers={layerDragHandlers}
                    />
                  );
                })}
              </div>
            </div>
          ))}

          <Playhead pxPerFrame={effectivePx} labelWidth={labelWidth} height={totalHeight + 26} scrubHandlers={scrubHandlers} />
          <div className="draghud" ref={hudRef} style={{ display: "none" }} />
          <div className="draghud" ref={layerHudRef} style={{ display: "none" }} />
        </div>
      </div>
    </div>
  );
}
