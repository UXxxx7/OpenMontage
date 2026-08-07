import { useCallback, useLayoutEffect, useRef } from "react";

/**
 * Drag-to-resize for a CSS custom property that controls a panel's size —
 * used for the boundary between the preview and the edit area (timeline on
 * desktop, preview on the phone shell — see `growsWhenDraggedUp` below for
 * why the two pass a different value here despite both handles living at
 * the same visual boundary).
 *
 * Mirrors useClipDrag.ts's own drag contract on purpose: Pointer Events +
 * setPointerCapture, the size written straight to the DOM as an inline
 * custom property during the drag (never React state — nothing here needs
 * to re-render on every pixel of movement, and writing state would trigger
 * exactly that), with the final value persisted to localStorage once, on
 * pointerup. Reading the size back out is not this hook's job: every
 * consumer is a CSS rule referencing `var(--the-custom-property)`, so
 * nothing downstream needs the numeric value as a React value at all.
 */
export function useResizablePanel({
  storageKey,
  cssVar,
  targetRef,
  defaultPx,
  minPx,
  maxPx,
  growsWhenDraggedUp = true,
}: {
  /** localStorage key this panel's size is remembered under. */
  storageKey: string;
  /** The CSS custom property name (e.g. "--timeline-h") that some
   *  descendant (or this same element) sizes itself from. */
  cssVar: string;
  /** The element the custom property is set ON — must be an ancestor
   *  (usually the shell root) of whatever CSS rule consumes `cssVar`,
   *  since custom properties resolve via inheritance. */
  targetRef: React.RefObject<HTMLElement | null>;
  /** A function when the sensible default is viewport-relative (e.g. the
   *  phone preview's old `40dvh` behavior for a first-time visitor with
   *  nothing in localStorage yet) — a plain number otherwise. */
  defaultPx: number | (() => number);
  minPx: number;
  /** A function, not a plain number — the ceiling depends on the current
   *  viewport height, which can change (window resize, orientation). */
  maxPx: () => number;
  /** Whether dragging the handle UP should GROW the panel `cssVar`
   *  controls. True when that panel sits BELOW the handle (desktop's
   *  timeline — dragging its top edge up grows it). False when it sits
   *  ABOVE the handle instead (the phone shell's preview — dragging the
   *  handle below it up should shrink the preview, growing the timeline
   *  through its own `flex: 1` rather than through a variable of its own).
   *  Defaults to true, the more common "resize the thing below" case. */
  growsWhenDraggedUp?: boolean;
}) {
  const dragRef = useRef<{ startY: number; startPx: number; pointerId: number } | null>(null);

  const resolveDefault = useCallback(
    () => (typeof defaultPx === "function" ? defaultPx() : defaultPx),
    [defaultPx]
  );

  const readStored = useCallback((): number => {
    if (typeof window === "undefined") return resolveDefault();
    const raw = window.localStorage.getItem(storageKey);
    const n = raw ? parseFloat(raw) : NaN;
    return Number.isFinite(n) ? n : resolveDefault();
  }, [storageKey, resolveDefault]);

  // Applied via useLayoutEffect (before paint), not useEffect — otherwise a
  // returning user who resized this panel last session would see one frame
  // at the CSS default before it snaps to their remembered size.
  useLayoutEffect(() => {
    const el = targetRef.current;
    if (!el) return;
    const clamped = Math.max(minPx, Math.min(maxPx(), readStored()));
    el.style.setProperty(cssVar, `${clamped}px`);
    // Deliberately only on mount — maxPx() is a live viewport-height read,
    // re-running this on every render would fight the user's own drag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const el = targetRef.current;
      if (!el) return;
      e.preventDefault();
      e.currentTarget.setPointerCapture(e.pointerId);
      const current = parseFloat(getComputedStyle(el).getPropertyValue(cssVar)) || readStored();
      dragRef.current = { startY: e.clientY, startPx: current, pointerId: e.pointerId };
    },
    [targetRef, cssVar, readStored]
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      const el = targetRef.current;
      if (!d || !el) return;
      const up = d.startY - e.clientY; // dragging up = positive
      const dy = growsWhenDraggedUp ? up : -up;
      const next = Math.max(minPx, Math.min(maxPx(), d.startPx + dy));
      el.style.setProperty(cssVar, `${next}px`);
    },
    [targetRef, minPx, maxPx, cssVar, growsWhenDraggedUp]
  );

  const finish = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      dragRef.current = null;
      if (!d) return;
      try {
        e.currentTarget.releasePointerCapture(d.pointerId);
      } catch {
        /* already released */
      }
      const el = targetRef.current;
      if (!el || typeof window === "undefined") return;
      const finalPx = parseFloat(getComputedStyle(el).getPropertyValue(cssVar));
      if (Number.isFinite(finalPx)) {
        window.localStorage.setItem(storageKey, String(finalPx));
      }
    },
    [targetRef, cssVar, storageKey]
  );

  return {
    /** Spread onto the drag-handle element. */
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: finish,
      onPointerCancel: finish,
    },
  };
}
