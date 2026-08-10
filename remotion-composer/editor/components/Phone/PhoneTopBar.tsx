import React, { useState } from "react";
import type { SaveState } from "../Toolbar";

/**
 * Compact top bar for the phone shell (Phase 6, F2b) — fixes a real,
 * confirmed bug in the desktop Toolbar at phone widths: that bar is a
 * non-wrapping flex row whose min-content (~480px) exceeds a 390px
 * viewport, with `body { overflow: hidden }` making the overflow
 * unreachable — "Save & send to WhatsApp" could be clipped off-screen
 * entirely. This bar uses `flex-wrap` and keeps Save in its own
 * always-fits row, so it is never the thing that gets pushed off.
 *
 * "Auto-tidy layout" moves into a small overflow menu here — it's a real,
 * useful action but not one of the phone shell's five primary ones, and
 * giving it a full-width button would crowd out Save on narrow screens.
 *
 * `onRelayout`/quota fields are optional (Phase 5 of the Arm B parity
 * work) — Arm A always supplies them; Arm B has no relayout concept and
 * never fetches a saves-per-hour quota client-side, so its shell omits
 * both rather than passing meaningless placeholder values. Omitting
 * `onRelayout` hides the "⋯" overflow menu entirely (today it's the menu's
 * only entry).
 */
export function PhoneTopBar({
  jobId,
  jobStatus,
  isDirty,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onSave,
  onRelayout,
  saveState,
  relayoutBusy,
  savesThisHour,
  savesPerHour,
  slotBusy,
}: {
  jobId: string;
  jobStatus: string;
  isDirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onSave: () => void;
  onRelayout?: () => void;
  saveState: SaveState;
  relayoutBusy?: boolean;
  savesThisHour?: number | null;
  savesPerHour?: number;
  slotBusy?: boolean;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const busy = saveState === "saving" || saveState === "rendering";
  const remaining = savesThisHour == null || savesPerHour == null ? null : Math.max(0, savesPerHour - savesThisHour);
  const outOfSaves = remaining !== null && remaining <= 0;

  return (
    <div className="phone-topbar">
      <div className="phone-topbar__row">
        <span
          className={`statusdot statusdot--${busy ? "busy" : isDirty ? "dirty" : "clean"}`}
          title={`Job status: ${jobStatus}`}
        />
        <span className="phone-topbar__title">{jobId}</span>

        <button type="button" className="btn btn--icon btn--ghost" onClick={onUndo} disabled={!canUndo} title="Undo">↶</button>
        <button type="button" className="btn btn--icon btn--ghost" onClick={onRedo} disabled={!canRedo} title="Redo">↷</button>

        {onRelayout && (
          <div className="phone-topbar__menu">
            <button
              type="button"
              className="btn btn--icon btn--ghost"
              onClick={() => setMenuOpen((v) => !v)}
              aria-label="More actions"
            >
              ⋯
            </button>
            {menuOpen && (
              <div className="phone-topbar__menu-popup">
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => { setMenuOpen(false); onRelayout(); }}
                  disabled={relayoutBusy || busy}
                >
                  {relayoutBusy ? "Tidying…" : "Auto-tidy layout"}
                </button>
                {slotBusy && <div className="field__hint">Queue busy — another job is rendering first.</div>}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="phone-topbar__row">
        {remaining !== null && (
          <span className="toolbar__meta" title="The server limits how many renders you can trigger per hour.">
            {remaining}/{savesPerHour} saves left
          </span>
        )}
        <span className="toolbar__spacer" />
        <button
          type="button"
          className="btn btn--primary"
          onClick={onSave}
          disabled={!isDirty || busy || outOfSaves}
          title={
            outOfSaves ? "Hourly render limit reached — try again later"
              : !isDirty ? "No changes to save"
              : "Render your edits and send the new preview to WhatsApp"
          }
        >
          {busy ? "Working…" : "Save & send"}
        </button>
      </div>
    </div>
  );
}
