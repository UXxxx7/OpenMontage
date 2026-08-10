import React, { useCallback, useEffect, useRef, useState } from "react";
import { authoredHitTestAt, isVisibleAtFrame, sameIdStack, type ManifestElement } from "../../state/authoredHitTest";
import { frameRef, subscribePlayhead } from "../../state/playhead";

function detectDesktopPointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return true;
  return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
}

const NATIVE_W = 1080;
const NATIVE_H = 1920;
const DRAG_THRESHOLD_PX = 3;
const MIN_SIZE = 40;
const CLICK_CYCLE_RADIUS_PX = 14;

type DragMode = "move" | "resize";

interface DragState {
  mode: DragMode;
  startClientX: number;
  startClientY: number;
  origX: number;
  origY: number;
  origW: number;
  origH: number;
  el: HTMLElement;
  moved: boolean;
}

export type EffectiveRect = { x: number; y: number; w: number; h: number };

export function effectiveRect(el: ManifestElement, overrides: Record<string, unknown> | undefined): EffectiveRect {
  const o = overrides || {};
  return {
    x: typeof o.x === "number" ? o.x : el.x,
    y: typeof o.y === "number" ? o.y : el.y,
    w: typeof o.w === "number" ? o.w : el.w,
    h: typeof o.h === "number" ? o.h : el.h,
  };
}

/**
 * Arm B's equivalent of components/Preview/PreviewOverlay.tsx — click to
 * select + drag to reposition/resize, same Pointer Events contract (in-flight
 * writes straight to the DOM, one commit on pointerup, never React state
 * mid-drag). Differences from the Arm A version:
 *  - Time-window visibility DOES gate hit-testing and the selection box —
 *    a real bug, not a polish gap (see authoredHitTest.ts's own comment):
 *    several manifest entries commonly share the same x/y/w/h footprint
 *    (sequential beats reusing one screen region), so without a frame
 *    filter, clicking a visible card could silently select a different,
 *    not-yet-mounted element at the same spot — every click looked like it
 *    did nothing. Subscribes to the same module-level frame store
 *    PreviewPane.tsx/Timeline use (state/playhead.ts) — unlike that
 *    composition (~40 components, no memo), this overlay only ever renders
 *    a handful of divs (one per manifest element), so tracking frame via
 *    plain React state (re-rendering this overlay every frame during
 *    playback) is cheap here in a way it deliberately isn't for Arm A's
 *    PreviewPane/PreviewOverlay pair.
 *  - Every element visible at the current frame gets a dashed outline
 *    (not just the selected one) — with several same-footprint,
 *    different-time cards, there was previously no visual hint that
 *    anything on screen was clickable at all.
 *  - One corner handle resizes both w AND h together (manifest elements
 *    have both, unlike Arm A's width-only draggable cards), not a
 *    width-only edge handle.
 *  - No snap-to-other-elements — the manifest's element count is typically
 *    small (a handful of graphic beats per scene, not ~22 content cards),
 *    so alignment snapping matters less; can be added later if it turns out
 *    to be missed.
 */
export function AuthoredOverlay({
  scale,
  manifest,
  overrides,
  selectedId,
  onSelect,
  onCommit,
}: {
  /** CSS px per composition px — matches AuthoredEditor's own measured player box. */
  scale: number | null;
  manifest: ManifestElement[];
  overrides: Record<string, Record<string, unknown>>;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onCommit: (id: string, patch: { x?: number; y?: number; w?: number; h?: number }) => void;
}) {
  const [isDesktopPointer] = useState(detectDesktopPointer);
  const rootRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const clickCycleRef = useRef<{ clientX: number; clientY: number; stack: string[]; cursor: number } | null>(null);

  const [frame, setFrame] = useState(() => frameRef.current);
  useEffect(() => subscribePlayhead(setFrame), []);
  const [hoverId, setHoverId] = useState<string | null>(null);

  const selectedEl = selectedId ? manifest.find((m) => m.id === selectedId) ?? null : null;
  const rect = selectedEl ? effectiveRect(selectedEl, overrides[selectedEl.id]) : null;
  const selectedVisible = !!selectedEl && isVisibleAtFrame(selectedEl, frame);

  const compClientToComposition = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const root = rootRef.current;
      if (!root || !scale) return null;
      const r = root.getBoundingClientRect();
      return { x: (clientX - r.left) / scale, y: (clientY - r.top) / scale };
    },
    [scale]
  );

  const handleCanvasClick = useCallback(
    (clientX: number, clientY: number) => {
      const comp = compClientToComposition(clientX, clientY);
      if (!comp) return;
      const stack = authoredHitTestAt(manifest, comp.x, comp.y, frame);

      const prev = clickCycleRef.current;
      const sameSpot =
        !!prev &&
        Math.hypot(clientX - prev.clientX, clientY - prev.clientY) <= CLICK_CYCLE_RADIUS_PX &&
        sameIdStack(stack, prev.stack);

      const cursor = sameSpot && stack.length > 0 ? (prev!.cursor + 1) % stack.length : 0;
      clickCycleRef.current = { clientX, clientY, stack, cursor };
      onSelect(stack.length > 0 ? stack[cursor] : null);
    },
    [compClientToComposition, manifest, frame, onSelect]
  );

  // Hover preview of what a click would grab — desktop only (no "hover" on
  // touch), mirrors PreviewOverlay.tsx's own handleCanvasPointerMove.
  const handleCanvasPointerMove = useCallback(
    (clientX: number, clientY: number) => {
      if (!isDesktopPointer) return;
      const comp = compClientToComposition(clientX, clientY);
      if (!comp) return;
      const stack = authoredHitTestAt(manifest, comp.x, comp.y, frame);
      const top = stack[0] ?? null;
      setHoverId((cur) => (cur === top ? cur : top));
    },
    [isDesktopPointer, compClientToComposition, manifest, frame]
  );
  const clearHover = useCallback(() => setHoverId((cur) => (cur ? null : cur)), []);

  const onPointerDown = useCallback(
    (mode: DragMode) => (e: React.PointerEvent<HTMLElement>) => {
      if (!scale || !rect || !selectedId) return;
      e.stopPropagation();
      e.preventDefault();
      const el = e.currentTarget as HTMLElement;
      // Can throw (NotFoundError) when the pointerdown didn't originate from
      // a real OS-level pointer session — harmless here since this box is
      // absolutely-positioned under our own control regardless of capture;
      // capture is a nice-to-have (keeps the drag tracking outside the
      // element's bounds), not required for the drag math itself to work.
      try { el.setPointerCapture(e.pointerId); } catch { /* not critical */ }
      dragRef.current = {
        mode,
        startClientX: e.clientX,
        startClientY: e.clientY,
        origX: rect.x,
        origY: rect.y,
        origW: rect.w,
        origH: rect.h,
        el,
        moved: false,
      };
    },
    [scale, rect, selectedId]
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      if (!d || !scale) return;
      const dxPx = e.clientX - d.startClientX;
      const dyPx = e.clientY - d.startClientY;
      if (!d.moved && Math.hypot(dxPx, dyPx) < DRAG_THRESHOLD_PX) return;
      d.moved = true;

      const dxComp = dxPx / scale;
      const dyComp = dyPx / scale;
      const box = boxRef.current;
      if (!box) return;

      if (d.mode === "resize") {
        const w = Math.max(MIN_SIZE, d.origW + dxComp);
        const h = Math.max(MIN_SIZE, d.origH + dyComp);
        box.style.width = `${w * scale}px`;
        box.style.height = `${h * scale}px`;
        (d as unknown as { lastW: number; lastH: number }).lastW = w;
        (d as unknown as { lastW: number; lastH: number }).lastH = h;
        return;
      }

      let x = d.origX + dxComp;
      let y = d.origY + dyComp;
      x = Math.max(0, Math.min(x, NATIVE_W - d.origW));
      y = Math.max(0, Math.min(y, NATIVE_H - d.origH));
      box.style.transform = `translate(${(x - d.origX) * scale}px, ${(y - d.origY) * scale}px)`;
      (d as unknown as { lastX: number; lastY: number }).lastX = x;
      (d as unknown as { lastX: number; lastY: number }).lastY = y;
    },
    [scale]
  );

  const finish = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      dragRef.current = null;
      if (!d) return;
      try {
        d.el.releasePointerCapture(e.pointerId);
      } catch {
        /* already released */
      }

      const dd = d as unknown as { lastX?: number; lastY?: number; lastW?: number; lastH?: number };
      const box = boxRef.current;
      if (d.moved && box && scale) {
        // Paint the final committed geometry directly, same reasoning as
        // PreviewOverlay's own `finish` — avoids a one-frame flash back to
        // stale geometry while waiting for the next prop update to arrive.
        const finalX = dd.lastX ?? d.origX;
        const finalY = dd.lastY ?? d.origY;
        const finalW = dd.lastW ?? d.origW;
        const finalH = dd.lastH ?? d.origH;
        box.style.transform = "";
        box.style.left = `${finalX * scale}px`;
        box.style.top = `${finalY * scale}px`;
        box.style.width = `${finalW * scale}px`;
        box.style.height = `${finalH * scale}px`;
      }

      if (!d.moved) {
        if (d.mode === "move") handleCanvasClick(e.clientX, e.clientY);
        return;
      }
      if (!selectedId) return;

      const patch: { x?: number; y?: number; w?: number; h?: number } = {};
      if (d.mode === "resize") {
        if (dd.lastW != null) patch.w = Math.round(dd.lastW);
        if (dd.lastH != null) patch.h = Math.round(dd.lastH);
      } else {
        if (dd.lastX != null) patch.x = Math.round(dd.lastX);
        if (dd.lastY != null) patch.y = Math.round(dd.lastY);
      }
      onCommit(selectedId, patch);
    },
    [selectedId, onCommit, scale, handleCanvasClick]
  );

  const rootDownRef = useRef<{ clientX: number; clientY: number } | null>(null);
  const onRootPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    rootDownRef.current = { clientX: e.clientX, clientY: e.clientY };
  }, []);
  const onRootPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const start = rootDownRef.current;
      rootDownRef.current = null;
      if (!start) return;
      if (Math.hypot(e.clientX - start.clientX, e.clientY - start.clientY) > DRAG_THRESHOLD_PX) return;
      handleCanvasClick(e.clientX, e.clientY);
    },
    [handleCanvasClick]
  );

  const onRootPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      // Never setState mid-drag — a drag already writes straight to the DOM
      // (onPointerMove above); re-rendering this component on every pointer
      // pixel during a drag would be pure waste, not just for hover.
      if (dragRef.current) return;
      handleCanvasPointerMove(e.clientX, e.clientY);
    },
    [handleCanvasPointerMove]
  );

  const rootHandlers = {
    ref: rootRef,
    onPointerDown: onRootPointerDown,
    onPointerUp: onRootPointerUp,
    onPointerCancel: () => { rootDownRef.current = null; },
    onPointerMove: onRootPointerMove,
    onPointerLeave: clearHover,
  };

  // Dashed outline on every element actually on screen right now — the
  // whole point is to show the user something here is clickable, since
  // several elements commonly share the same footprint at different times
  // and there's otherwise no visual hint any of this is interactive.
  const visibleOutlines = scale
    ? manifest
        .filter((el) => el.id !== selectedId && isVisibleAtFrame(el, frame))
        .map((el) => {
          const r = effectiveRect(el, overrides[el.id]);
          return (
            <div
              key={el.id}
              className={`preview-overlay__hover${el.id === hoverId ? " preview-overlay__hover--active" : ""}`}
              style={{
                position: "absolute",
                left: r.x * scale,
                top: r.y * scale,
                width: r.w * scale,
                height: r.h * scale,
                pointerEvents: "none",
              }}
            />
          );
        })
    : null;

  if (!scale || !rect || !selectedId || !selectedVisible) {
    return (
      <div className="preview-overlay" style={{ position: "absolute", inset: 0, pointerEvents: "auto" }} {...rootHandlers}>
        {visibleOutlines}
      </div>
    );
  }

  return (
    <div className="preview-overlay" style={{ position: "absolute", inset: 0, pointerEvents: "auto" }} {...rootHandlers}>
      {visibleOutlines}
      <div
        ref={boxRef}
        className="preview-overlay__box"
        style={{
          position: "absolute",
          left: rect.x * scale,
          top: rect.y * scale,
          width: rect.w * scale,
          height: rect.h * scale,
          pointerEvents: "auto",
        }}
        onPointerDown={onPointerDown("move")}
        onPointerMove={onPointerMove}
        onPointerUp={finish}
        onPointerCancel={finish}
      >
        {isDesktopPointer && (
          <span
            className="preview-overlay__handle preview-overlay__handle--corner"
            onPointerDown={onPointerDown("resize")}
            onPointerMove={onPointerMove}
            onPointerUp={finish}
            onPointerCancel={finish}
          />
        )}
      </div>
    </div>
  );
}
