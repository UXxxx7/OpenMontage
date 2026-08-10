import { useCallback, useRef, type MutableRefObject } from "react";
import { flushSync } from "react-dom";

const MIN_PX_PER_FRAME = 0.05;
const MAX_PX_PER_FRAME = 40;

type Point = { x: number; y: number };

const dist = (pts: Point[]): number => Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);

/**
 * Two-finger pinch-to-zoom for the Timeline (Phase 6, F3b). Attached as
 * CAPTURE-phase handlers on the scroll container (`.timeline__scroll`), not
 * on individual clips — capture fires before a clip's own onPointerDown, so
 * the second finger of a pinch can be intercepted (and any in-flight
 * single-pointer clip/layer drag cancelled) no matter which element it
 * happens to land on, not just empty track.
 *
 * pxPerFrame drives dozens of elements at once here (every clip, the
 * ruler, the playhead, splice markers) — unlike useClipDrag's own
 * DOM-write-in-flight pattern, re-deriving all of that outside React per
 * pointermove isn't practical. Instead this throttles to one React state
 * update per animation frame via flushSync, so the scroll-position
 * correction that keeps content anchored under the fingers can read the
 * just-committed layout synchronously in the same callback.
 */
export function usePinchZoom({
  scrollRef,
  labelWidth,
  pxPerFrameRef,
  setPxPerFrame,
  cancelActiveDrags,
}: {
  scrollRef: React.RefObject<HTMLDivElement | null>;
  labelWidth: number;
  /** Read via a ref rather than closing over the prop value: the rAF
   *  callback below can fire after several re-renders already happened
   *  this gesture, and a stale closure would use a stale zoom level as the
   *  base for the NEXT frame's ratio, making the zoom rate wrong. */
  pxPerFrameRef: MutableRefObject<number>;
  setPxPerFrame: (next: number) => void;
  /** Called the instant a second finger comes down, so useClipDrag/
   *  useLayerDrag's own dragRef doesn't keep committing moves while the
   *  user pinches. */
  cancelActiveDrags: () => void;
}) {
  const pointers = useRef(new Map<number, Point>());
  const gestureStart = useRef<{ dist: number; pxPerFrame: number } | null>(null);
  const rafPending = useRef(false);

  const applyPinch = useCallback(() => {
    rafPending.current = false;
    const scrollEl = scrollRef.current;
    const start = gestureStart.current;
    if (!scrollEl || !start || pointers.current.size < 2) return;

    const pts = Array.from(pointers.current.values());
    const midX = (pts[0].x + pts[1].x) / 2;
    const ratio = dist(pts) / start.dist;
    const next = Math.max(MIN_PX_PER_FRAME, Math.min(MAX_PX_PER_FRAME, start.pxPerFrame * ratio));

    const rect = scrollEl.getBoundingClientRect();
    const oldPxPerFrame = pxPerFrameRef.current || next;
    // The frame currently sitting under the fingers' midpoint, in the
    // OLD zoom level — this is what must stay under the midpoint once the
    // new pxPerFrame takes effect.
    const anchorFrame = (midX - rect.left + scrollEl.scrollLeft - labelWidth) / oldPxPerFrame;

    flushSync(() => setPxPerFrame(next));

    scrollEl.scrollLeft = Math.max(0, labelWidth + anchorFrame * next - (midX - rect.left));
  }, [scrollRef, labelWidth, pxPerFrameRef, setPxPerFrame]);

  const onPointerDownCapture = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerType !== "touch") return;
      pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.current.size >= 2) {
        // Claim the gesture before it reaches the clip under this finger —
        // otherwise a second finger landing on an unselected clip would
        // fire that clip's onPointerDown and select it as a side effect of
        // pinching, not a deliberate tap.
        e.stopPropagation();
      }
      if (pointers.current.size === 2) {
        cancelActiveDrags();
        const pts = Array.from(pointers.current.values());
        gestureStart.current = { dist: dist(pts), pxPerFrame: pxPerFrameRef.current };
      }
    },
    [cancelActiveDrags, pxPerFrameRef]
  );

  const onPointerMoveCapture = useCallback(
    (e: React.PointerEvent) => {
      if (!pointers.current.has(e.pointerId)) return;
      pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.current.size < 2 || !gestureStart.current) return;
      e.preventDefault();
      e.stopPropagation();
      if (!rafPending.current) {
        rafPending.current = true;
        requestAnimationFrame(applyPinch);
      }
    },
    [applyPinch]
  );

  const endPointer = useCallback((e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) gestureStart.current = null;
  }, []);

  return {
    /** Spread onto `.timeline__scroll` alongside its existing handlers. */
    pinchHandlers: {
      onPointerDownCapture,
      onPointerMoveCapture,
      onPointerUpCapture: endPointer,
      onPointerCancelCapture: endPointer,
    },
  };
}
