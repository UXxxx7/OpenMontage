import React from "react";

export type SaveState = "idle" | "saving" | "rendering" | "done" | "failed";

export function Toolbar({
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
  onExport,
}: {
  jobId: string;
  jobStatus: string;
  isDirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onSave: () => void;
  onRelayout: () => void;
  saveState: SaveState;
  relayoutBusy: boolean;
  savesThisHour: number | null;
  savesPerHour: number;
  slotBusy: boolean;
  onExport: () => void;
}) {
  const busy = saveState === "saving" || saveState === "rendering";
  const remaining = savesThisHour === null ? null : Math.max(0, savesPerHour - savesThisHour);
  const outOfSaves = remaining !== null && remaining <= 0;

  return (
    <div className="toolbar app__toolbar">
      <span className="toolbar__title">OpenMontage</span>
      <span className="toolbar__meta">{jobId}</span>

      <div className="toolbar__group" style={{ marginLeft: 10 }}>
        <button type="button" className="btn btn--icon btn--ghost" onClick={onUndo}
          disabled={!canUndo} title="Undo (Ctrl+Z)">↶</button>
        <button type="button" className="btn btn--icon btn--ghost" onClick={onRedo}
          disabled={!canRedo} title="Redo (Ctrl+Shift+Z)">↷</button>
      </div>

      <button type="button" className="btn" onClick={onRelayout} disabled={relayoutBusy || busy}
        title="Recompute card positions to avoid the facecam. Preview only — undoable, and not saved until you hit Save.">
        {relayoutBusy ? "Tidying…" : "Auto-tidy layout"}
      </button>

      <span className="toolbar__spacer" />

      <span className="toolbar__meta" title={`Job status: ${jobStatus}`}>
        <span className={`statusdot statusdot--${busy ? "busy" : isDirty ? "dirty" : "clean"}`} />
        {" "}
        {busy ? (saveState === "saving" ? "Saving…" : "Rendering…") : isDirty ? "Unsaved changes" : "Saved"}
      </span>

      {remaining !== null && (
        <span className="toolbar__meta" title="The server limits how many renders you can trigger per hour.">
          {remaining}/{savesPerHour} saves left
        </span>
      )}
      {slotBusy && (
        <span className="toolbar__meta" title="Another job is rendering on the machine — yours will start after it.">
          queue busy
        </span>
      )}

      <button
        type="button"
        className="btn"
        onClick={onExport}
        title="Download the finished video directly — pick resolution and file size"
      >
        Export
      </button>

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
        {busy ? "Working…" : "Save & send to WhatsApp"}
      </button>
    </div>
  );
}
