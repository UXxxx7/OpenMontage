import { useCallback, useRef } from "react";
import type { ClipItem } from "../../state/model";
import { framesToTimecode } from "../../state/playhead";

export type DragMode = "move" | "resize-l" | "resize-r";

type DragState = {
  mode: DragMode;
  clip: ClipItem;
  startX: number;
  origFrom: number;
  origTo: number;
  /** The element that received pointerdown — pointer capture MUST stay on
   *  this one (release/capture target), or a resize handle stops receiving
   *  pointermove once the pointer moves off its own tiny hit area. */
  captureEl: HTMLElement;
  /** The element whose inline style actually represents the clip's in-flight
   *  position/size, and where the HUD anchors. For "move" this is the same
   *  element as captureEl (the clip bar itself receives that pointerdown).
   *  For "resize-l"/"resize-r" the pointerdown lands on a 6px handle SPAN
   *  nested inside the clip, not the clip bar — writing style here instead
   *  of on captureEl is the fix for the resize handle visually moving
   *  backwards (see Clip.tsx's clipElRef). */
  styleEl: HTMLElement;
  pointerId: number;
  moved: boolean;
  lastFrom: number;
  lastTo: number;
};

const SNAP_RADIUS_PX = 8;
/** 小于这个位移不算拖拽——避免"想点选却轻微抖了一下"被当成一次改动。 */
const DRAG_THRESHOLD_PX = 3;

/**
 * clip 拖拽/改长度。
 *
 * 三条关键决定：
 * 1. **拖拽过程中的位置绝不进 React state**——那正是拖拽发抖的根源。
 *    pointermove 直接写 DOM（transform / width），pointerup 才提交一次。
 * 2. **Pointer Events + setPointerCapture**，不是 mouse events：鼠标/触屏/手写笔
 *    走同一条代码路径，且指针移出窗口也不会丢事件。
 * 3. **吸附半径按像素固定（8px），换算成帧**（`8 / pxPerFrame`）——这样无论
 *    放大到什么倍数，手感都一致；如果按帧数固定，放大后会黏得让人抓狂。
 */
export function useClipDrag({
  pxPerFrame,
  durationInFrames,
  getSnapTargets,
  onCommit,
  onSelect,
  hudRef,
  isDragAllowed,
}: {
  pxPerFrame: number;
  durationInFrames: number;
  /** 同 lane 的其它 clip 边缘 + 章节点 + 播放头，用于吸附。 */
  getSnapTargets: (clip: ClipItem) => number[];
  onCommit: (clip: ClipItem, edit: { from?: number; to?: number }) => void;
  onSelect: (clip: ClipItem) => void;
  hudRef: React.RefObject<HTMLDivElement | null>;
  /** Per-clip, not a single flag — on touch only the ALREADY-SELECTED clip
   *  may be dragged (Phase 6, F3a); an unselected clip must still let a
   *  swipe over it scroll the timeline. Desktop ignores selection entirely
   *  and always returns true. */
  isDragAllowed: (clip: ClipItem) => boolean;
}) {
  const dragRef = useRef<DragState | null>(null);

  const showHud = useCallback((el: HTMLElement, from: number, to: number, isPoint: boolean, openEnded?: boolean) => {
    const hud = hudRef.current;
    if (!hud) return;
    hud.style.display = "block";
    hud.textContent = isPoint
      ? framesToTimecode(from)
      : openEnded
      ? `${framesToTimecode(from)} → end`
      : `${framesToTimecode(from)} → ${framesToTimecode(to)}  (${((to - from) / 30).toFixed(2)}s)`;
    // 贴在被拖的 clip 正上方，跟着它一起动。
    const parent = hud.offsetParent as HTMLElement | null;
    if (parent) {
      const clipRect = el.getBoundingClientRect();
      const parentRect = parent.getBoundingClientRect();
      hud.style.left = `${clipRect.left - parentRect.left}px`;
      hud.style.top = `${clipRect.top - parentRect.top - 22}px`;
    }
  }, [hudRef]);

  const hideHud = useCallback(() => {
    if (hudRef.current) hudRef.current.style.display = "none";
  }, [hudRef]);

  const snap = useCallback((frame: number, targets: number[], disabled: boolean): number => {
    if (disabled) return frame;
    const radius = SNAP_RADIUS_PX / Math.max(pxPerFrame, 0.0001);
    let best = frame;
    let bestDist = radius;
    for (const t of targets) {
      const d = Math.abs(t - frame);
      if (d < bestDist) { bestDist = d; best = t; }
    }
    return best;
  }, [pxPerFrame]);

  const onPointerDown = useCallback((clip: ClipItem, mode: DragMode, elRef?: React.RefObject<HTMLElement | null>) =>
    (e: React.PointerEvent<HTMLElement>) => {
      // 选中永远要发生，哪怕这次拖拽被禁用（触屏上未选中的 clip）。
      onSelect(clip);
      if (!isDragAllowed(clip)) return;
      if (mode !== "move" && clip.isPoint) return; // 时间点没有长度可以拉
      e.stopPropagation();
      e.preventDefault();

      const captureEl = e.currentTarget as HTMLElement;
      captureEl.setPointerCapture(e.pointerId);
      dragRef.current = {
        mode, clip, captureEl,
        styleEl: elRef?.current ?? captureEl,
        pointerId: e.pointerId,
        startX: e.clientX,
        origFrom: clip.from,
        origTo: clip.to,
        moved: false,
        lastFrom: clip.from,
        lastTo: clip.to,
      };
    }, [isDragAllowed, onSelect]);

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const dxPx = e.clientX - d.startX;
    if (!d.moved && Math.abs(dxPx) < DRAG_THRESHOLD_PX) return;
    d.moved = true;

    const dxFrames = dxPx / pxPerFrame;
    const targets = getSnapTargets(d.clip);
    const noSnap = e.altKey; // 每次 move 都读——允许拖到一半再关掉吸附
    const dur = d.origTo - d.origFrom;

    let from = d.origFrom;
    let to = d.origTo;

    if (d.mode === "move") {
      from = snap(d.origFrom + dxFrames, targets, noSnap);
      from = Math.max(0, Math.min(from, durationInFrames - (d.clip.isPoint ? 0 : 1)));
      if (d.clip.isPoint) {
        to = from;
      } else if (d.clip.openEnded) {
        // Anchored to the end of the video by definition (no stored endFrame
        // to preserve) — moving it shrinks/grows its visible length instead
        // of the fixed-duration snap-back a real endFrame gets below.
        to = durationInFrames;
      } else {
        to = from + dur;
        if (to > durationInFrames) {
          to = durationInFrames;
          from = to - dur;
        }
      }
      d.styleEl.style.transform = `translateX(${(from - d.origFrom) * pxPerFrame}px)`;
      if (d.clip.openEnded && !d.clip.isPoint) {
        d.styleEl.style.width = `${Math.max(3, (to - from) * pxPerFrame)}px`;
      }
    } else if (d.mode === "resize-r") {
      to = snap(d.origTo + dxFrames, targets, noSnap);
      to = Math.max(from + 1, Math.min(to, durationInFrames));
      d.styleEl.style.width = `${Math.max(3, (to - from) * pxPerFrame)}px`;
    } else {
      from = snap(d.origFrom + dxFrames, targets, noSnap);
      from = Math.max(0, Math.min(from, d.origTo - 1));
      d.styleEl.style.transform = `translateX(${(from - d.origFrom) * pxPerFrame}px)`;
      d.styleEl.style.width = `${Math.max(3, (to - from) * pxPerFrame)}px`;
    }

    d.lastFrom = Math.round(from);
    d.lastTo = Math.round(to);
    showHud(d.styleEl, d.lastFrom, d.lastTo, d.clip.isPoint, d.mode === "move" && d.clip.openEnded);
  }, [pxPerFrame, durationInFrames, getSnapTargets, snap, showHud]);

  const finish = useCallback((e: React.PointerEvent<HTMLElement>) => {
    const d = dragRef.current;
    dragRef.current = null;
    hideHud();
    if (!d) return;
    try { d.captureEl.releasePointerCapture(e.pointerId); } catch { /* already released */ }

    // 还原内联样式——提交之后由 React 用新的 props 重新定位。
    d.styleEl.style.transform = "";
    d.styleEl.style.width = "";

    if (!d.moved) return;
    // **按帧比较，不是按毫秒/像素**：帧号没变就一个字节都不写。这是字幕
    // ms<->frame 往返漂移的主要防线（1000/30 = 33.333ms 不是整数）。
    const changedFrom = d.lastFrom !== d.origFrom;
    const changedTo = d.lastTo !== d.origTo;
    if (!changedFrom && !changedTo) return;

    if (d.mode === "move") {
      // Open-ended stays open on a move — only resize-r is allowed to
      // materialize an endFrame (that's the documented, deliberate way to
      // give an infinite clip a real end; see Clip.tsx's ∞ badge).
      const keepOpen = d.clip.isPoint || d.clip.openEnded;
      onCommit(d.clip, { from: d.lastFrom, to: keepOpen ? undefined : d.lastTo });
    } else if (d.mode === "resize-r") {
      onCommit(d.clip, { to: d.lastTo });
    } else {
      onCommit(d.clip, { from: d.lastFrom });
    }
  }, [hideHud, onCommit]);

  // A second finger landing anywhere in the timeline promotes the gesture
  // to a pinch (usePinchZoom.ts, Phase 6, F3b) — this drops the in-flight
  // drag WITHOUT committing it (unlike finish(), no onCommit call), since
  // the user's intent just changed from "move this clip" to "zoom the view."
  const cancel = useCallback(() => {
    const d = dragRef.current;
    dragRef.current = null;
    hideHud();
    if (!d) return;
    try { d.captureEl.releasePointerCapture(d.pointerId); } catch { /* already released */ }
    d.styleEl.style.transform = "";
    d.styleEl.style.width = "";
  }, [hideHud]);

  return {
    onPointerDown,
    cancelDrag: cancel,
    /** 挂在 clip 元素上——pointer capture 保证事件仍然回到这个元素。 */
    dragHandlers: {
      onPointerMove,
      onPointerUp: finish,
      onPointerCancel: finish,
    },
    isDragging: () => dragRef.current !== null,
  };
}
