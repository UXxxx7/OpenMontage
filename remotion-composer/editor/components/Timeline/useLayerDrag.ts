import { useCallback, useRef } from "react";
import type { LayerClipItem } from "../../state/layers";

type DragState = {
  clip: LayerClipItem;
  startY: number;
  origLayer: number;
  el: HTMLElement;
  pointerId: number;
  moved: boolean;
  lastLayer: number;
};

const DRAG_THRESHOLD_PX = 3;

/**
 * Vertical-only drag for the Timeline's "Layers" view — a clip's row IS its
 * `layer`, so dragging up/down changes stacking order. Deliberately a
 * SEPARATE hook from useClipDrag.ts rather than extending it to a second
 * axis: the two views show different things (by-type groups by section and
 * edits time; layers groups by z-order and edits stacking), so merging them
 * into one gesture would mean guessing whether a diagonal drag meant "move
 * in time" or "change layer." Same three decisions as useClipDrag.ts,
 * ported to the vertical axis: no React state while dragging (direct DOM
 * transform), Pointer Events + setPointerCapture, one commit on pointerup.
 */
export function useLayerDrag({
  rowHeight,
  maxLayer,
  onCommit,
  onSelect,
  hudRef,
  isDragAllowed,
}: {
  rowHeight: number;
  maxLayer: number;
  onCommit: (clip: LayerClipItem, newLayer: number) => void;
  onSelect: (clip: LayerClipItem) => void;
  hudRef: React.RefObject<HTMLDivElement | null>;
  /** Per-clip — see useClipDrag.ts's own doc. On touch only the
   *  already-selected clip becomes draggable (Phase 6, F3a). */
  isDragAllowed: (clip: LayerClipItem) => boolean;
}) {
  const dragRef = useRef<DragState | null>(null);

  const showHud = useCallback((el: HTMLElement, layer: number) => {
    const hud = hudRef.current;
    if (!hud) return;
    hud.style.display = "block";
    hud.textContent = `Layer ${layer}`;
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

  const onPointerDown = useCallback(
    (clip: LayerClipItem) => (e: React.PointerEvent<HTMLElement>) => {
      onSelect(clip);
      if (!isDragAllowed(clip)) return;
      e.stopPropagation();
      e.preventDefault();
      const el = e.currentTarget as HTMLElement;
      el.setPointerCapture(e.pointerId);
      dragRef.current = {
        clip, startY: e.clientY, origLayer: clip.layer, el,
        pointerId: e.pointerId, moved: false, lastLayer: clip.layer,
      };
    },
    [isDragAllowed, onSelect]
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      if (!d) return;
      const dy = e.clientY - d.startY;
      if (!d.moved && Math.abs(dy) < DRAG_THRESHOLD_PX) return;
      d.moved = true;

      const rowDelta = Math.round(dy / rowHeight);
      const newLayer = Math.max(0, Math.min(maxLayer, d.origLayer + rowDelta));
      d.el.style.transform = `translateY(${(newLayer - d.origLayer) * rowHeight}px)`;
      d.lastLayer = newLayer;
      showHud(d.el, newLayer);
    },
    [rowHeight, maxLayer, showHud]
  );

  const finish = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      dragRef.current = null;
      hideHud();
      if (!d) return;
      try {
        d.el.releasePointerCapture(e.pointerId);
      } catch {
        /* already released */
      }
      d.el.style.transform = "";
      if (!d.moved || d.lastLayer === d.origLayer) return;
      onCommit(d.clip, d.lastLayer);
    },
    [hideHud, onCommit]
  );

  // See useClipDrag.ts's own cancel() — same reasoning, ported to the
  // vertical axis: a second finger promotes the gesture to a pinch
  // (usePinchZoom.ts), dropping this drag without committing it.
  const cancel = useCallback(() => {
    const d = dragRef.current;
    dragRef.current = null;
    hideHud();
    if (!d) return;
    try { d.el.releasePointerCapture(d.pointerId); } catch { /* already released */ }
    d.el.style.transform = "";
  }, [hideHud]);

  return {
    onPointerDown,
    cancelDrag: cancel,
    dragHandlers: { onPointerMove, onPointerUp: finish, onPointerCancel: finish },
  };
}
