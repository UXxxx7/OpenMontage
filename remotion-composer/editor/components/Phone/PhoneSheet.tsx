import React, { useCallback, useRef } from "react";

const DRAG_TO_CLOSE_PX = 80;

/**
 * Bottom-sheet host for the phone shell's action bar (Phase 6, F2b) — Add /
 * Cut / Edit / Audio each open one of these over the timeline, rather than
 * navigating away from the video (CapCut's own pattern: the canvas and
 * timeline stay visible/scrubbable underneath while you work in a sheet).
 *
 * Deliberately dumb: no portal, no focus trap, no animation library — a
 * fixed-position panel, a backdrop that closes on tap, and a drag-down
 * gesture on the header. Mirrors this codebase's existing drag philosophy
 * (Pointer Events + setPointerCapture, position written to the DOM while
 * dragging, one commit — here "commit" just means close or snap back) even
 * though the stakes are much lower than a data-editing drag.
 */
export function PhoneSheet({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ startY: number; lastDy: number } | null>(null);

  const onHandlePointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    dragRef.current = { startY: e.clientY, lastDy: 0 };
  }, []);

  const onHandlePointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    const panel = panelRef.current;
    if (!d || !panel) return;
    const dy = Math.max(0, e.clientY - d.startY); // only allow dragging DOWN
    d.lastDy = dy;
    panel.style.transform = `translateY(${dy}px)`;
  }, []);

  const onHandlePointerUp = useCallback(() => {
    const d = dragRef.current;
    const panel = panelRef.current;
    dragRef.current = null;
    if (!d || !panel) return;
    if (d.lastDy > DRAG_TO_CLOSE_PX) {
      onClose();
      return;
    }
    panel.style.transform = "";
  }, [onClose]);

  return (
    <div className="phone-sheet">
      <div className="phone-sheet__backdrop" onPointerDown={onClose} />
      <div className="phone-sheet__panel" ref={panelRef}>
        <div
          className="phone-sheet__handle"
          onPointerDown={onHandlePointerDown}
          onPointerMove={onHandlePointerMove}
          onPointerUp={onHandlePointerUp}
          onPointerCancel={onHandlePointerUp}
        >
          <span className="phone-sheet__grip" />
        </div>
        <div className="phone-sheet__header">
          <span className="phone-sheet__title">{title}</span>
          <button type="button" className="btn btn--icon btn--ghost" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="phone-sheet__body">{children}</div>
      </div>
    </div>
  );
}
