import { useCallback, useRef } from "react";
import { setPlayheadFrame } from "../../state/playhead";

/**
 * Draggable playhead — replaces the click-only `seekFromEvent` duplicated
 * across Timeline.tsx (Arm A) and AuthoredTimeline.tsx (Arm B). Both used
 * to wire only `onPointerDown` on the ruler/empty-track areas: one discrete
 * seek per click, no scrub, no drag continuation. This hook adds
 * `onPointerMove`/`onPointerUp` + pointer capture so the same gesture that
 * used to seek once now continues tracking the pointer until release.
 *
 * Anchored to the `.timeline__scroll` container (not whichever element the
 * pointerdown happened to land on) so the frame math is identical
 * regardless of whether the drag started on the ruler, an empty lane
 * track, or the playhead's own grip — `x = clientX - scrollRect.left +
 * scrollEl.scrollLeft - labelWidth`, which is algebraically the same
 * formula the old per-call-site `originLeft` arguments each computed by
 * hand, just derived from one stable anchor instead of three slightly
 * different ones. `getBoundingClientRect()` is read fresh on every rAF
 * tick (not cached at pointerdown) since the container can itself scroll
 * mid-gesture (e.g. autoscroll, or the user scrolling with a second input
 * device) — a cached rect would drift out of sync with reality.
 *
 * Position is pushed straight into the module-level playhead store
 * (`setPlayheadFrame`) on every rAF tick, same as every other gesture in
 * this codebase (Playhead.tsx's own doc comment) — routing a 60fps drag
 * through React state would re-render the whole timeline every frame.
 * `onSeek` is also called every tick so the actual Player follows along
 * live, not just the visual line.
 */
export function usePlayheadScrub({
  scrollRef,
  labelWidth,
  pxPerFrameRef,
  durationInFrames,
  onSeek,
  onScrubStart,
  onScrubEnd,
}: {
  scrollRef: React.RefObject<HTMLElement | null>;
  labelWidth: number;
  /** Read via a ref, not the raw prop value — same reasoning as
   *  usePinchZoom's own pxPerFrameRef: an rAF callback can fire after
   *  several re-renders already happened this gesture, and a stale closure
   *  would use a stale zoom level. */
  pxPerFrameRef: React.RefObject<number>;
  durationInFrames: number;
  onSeek: (frame: number) => void;
  /** Playback doesn't pause itself when a scrub begins (seekTo alone
   *  doesn't stop playback) — callers use this to pause, and `onScrubEnd`
   *  to resume if it was playing before. A one-shot click never needed
   *  this; a continuous drag does, or the video keeps advancing under the
   *  user's finger while they're trying to park it on one frame. */
  onScrubStart?: () => void;
  onScrubEnd?: () => void;
}) {
  const dragging = useRef(false);
  const rafPending = useRef(false);
  const lastClientX = useRef(0);

  const frameFromClientX = useCallback((clientX: number): number => {
    const scrollEl = scrollRef.current;
    const pxPerFrame = pxPerFrameRef.current || 1;
    if (!scrollEl) return 0;
    const rect = scrollEl.getBoundingClientRect();
    const x = clientX - rect.left + scrollEl.scrollLeft - labelWidth;
    const frame = Math.round(x / pxPerFrame);
    return Math.max(0, Math.min(frame, Math.max(0, durationInFrames - 1)));
  }, [scrollRef, labelWidth, pxPerFrameRef, durationInFrames]);

  const applyFrame = useCallback((clientX: number) => {
    const frame = frameFromClientX(clientX);
    setPlayheadFrame(frame);
    onSeek(frame);
  }, [frameFromClientX, onSeek]);

  const scheduleApply = useCallback(() => {
    if (rafPending.current) return;
    rafPending.current = true;
    requestAnimationFrame(() => {
      rafPending.current = false;
      if (dragging.current) applyFrame(lastClientX.current);
    });
  }, [applyFrame]);

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    e.stopPropagation();
    dragging.current = true;
    lastClientX.current = e.clientX;
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* not critical */ }
    onScrubStart?.();
    applyFrame(e.clientX); // seek immediately on down — preserves today's single-click behavior
  }, [applyFrame, onScrubStart]);

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (!dragging.current) return;
    e.preventDefault();
    lastClientX.current = e.clientX;
    scheduleApply();
  }, [scheduleApply]);

  const endDrag = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (!dragging.current) return;
    dragging.current = false;
    try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId); } catch { /* already released */ }
    onScrubEnd?.();
  }, [onScrubEnd]);

  // A second finger promoting to a pinch (usePinchZoom.ts) must drop an
  // in-flight scrub the same way it drops an in-flight clip/layer drag —
  // called from the same cancelActiveDrags wiring, no onSeek/onScrubEnd
  // call (the gesture didn't complete, it was hijacked).
  const cancelScrub = useCallback(() => {
    dragging.current = false;
  }, []);

  return {
    /** Spread onto the ruler wrapper, empty lane tracks, and the
     *  Playhead's own grip — identical handlers everywhere on purpose, so
     *  a drag started on any of them continues seamlessly. */
    scrubHandlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp: endDrag,
      onPointerCancel: endDrag,
    },
    cancelScrub,
  };
}
