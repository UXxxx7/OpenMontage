import React, { useEffect, useRef } from "react";
import { ExportDialogBody } from "./ExportDialogBody";
import type { UseExportReturn } from "../../state/useExport";

/**
 * Desktop-only modal chrome around ExportDialogBody. `position: fixed`,
 * never a grid child — .app's grid-template-areas claims every named area,
 * and .app__center's own comment documents that an explicitly-placed child
 * added earlier in DOM order silently displaces the preview (see styles.css).
 * z-index 40: above .phone-sheet (30, though this component is desktop-only
 * and never mounts alongside it), below .toast (50) so a save-error toast
 * stays legible over an open Export dialog.
 */
export function ExportDialog({
  x, jobId, token, isDirty, onClose,
}: { x: UseExportReturn; jobId: string; token: string; isDirty: boolean; onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const firstFocusRef = useRef<HTMLButtonElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    firstFocusRef.current?.focus();
    return () => previouslyFocused.current?.focus();
  }, []);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div
      className="modal__backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Export video"
      // Close on a click that lands on the backdrop itself, not one that
      // bubbled up from inside the panel — the standard "click === currentTarget"
      // check, since a single element here is both the backdrop and the click target.
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="modal__panel" ref={panelRef}>
        <div className="modal__header">
          <span className="modal__title">Export</span>
          <button ref={firstFocusRef} type="button" className="btn btn--icon btn--ghost" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="modal__body">
          <ExportDialogBody x={x} jobId={jobId} token={token} isDirty={isDirty} />
        </div>
      </div>
    </div>
  );
}
