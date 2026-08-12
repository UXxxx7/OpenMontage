import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "../api";

export type ExportResolution = "1080p" | "720p";
export type ExportQuality = "high" | "balanced" | "small";
export type ExportState = "idle" | "requesting" | "encoding" | "done" | "failed";

export type ExportCombo = {
  resolution: ExportResolution;
  quality: ExportQuality;
  width: number;
  height: number;
  estimated_bytes: number;
  fits_whatsapp: boolean;
  filename: string;
  cached: boolean;
  cached_bytes: number | null;
};

export type ExportOptions = {
  source: { width: number; height: number; fps: number; duration_seconds: number; bytes: number; modified_at: number };
  whatsapp_limit_bytes: number;
  combos: ExportCombo[];
  save_state: string;
  exports_this_hour: number;
  exports_per_hour: number;
  export_slot_busy: boolean;
};

/**
 * The whole in-editor "Export" state machine — one instance shared by both
 * arms (App.tsx's Editor for Arm A, AuthoredEditor.tsx for Arm B), same
 * split as SaveState: the owning component keeps isDirty/saveState/onSave,
 * this hook owns everything export-specific. Kept as ONE hook (not spread
 * across ExportDialogBody's own state) so mobile's PhoneSheet and desktop's
 * ExportDialog — two different chrome wrapping the same body content — never
 * have their own diverging copies of "what combo is selected" or "how far
 * along is the encode."
 *
 * Dialog state survives being closed mid-encode: closing just hides the
 * dialog, it doesn't cancel the poll — reopening resumes wherever progress
 * actually is, and completion still fires the caller's onDone (wired to the
 * existing showToast in both arms) even while closed.
 */
export function useExport({
  jobId,
  token,
  isDirty,
  saveState,
  onSave,
  onDone,
}: {
  jobId: string;
  token: string;
  isDirty: boolean;
  /** The owning editor's own save state ("idle"|"saving"|"rendering"|"done"|"failed") —
   *  loosely typed here since Arm A and Arm B each declare their own SaveState alias. */
  saveState: string;
  onSave: () => void;
  onDone?: (msg: string) => void;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [options, setOptions] = useState<ExportOptions | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);

  const [resolution, setResolution] = useState<ExportResolution>("1080p");
  const [quality, setQuality] = useState<ExportQuality>("balanced");

  const [exportState, setExportState] = useState<ExportState>("idle");
  const [progress, setProgress] = useState(0);
  const [filename, setFilename] = useState<string | null>(null);
  const [bytes, setBytes] = useState<number | null>(null);
  const [fitsWhatsapp, setFitsWhatsapp] = useState<boolean | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const [pendingAfterSave, setPendingAfterSave] = useState(false);
  const pollRef = useRef<number | null>(null);

  const clearPoll = useCallback(() => {
    if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
  }, []);
  useEffect(() => () => clearPoll(), [clearPoll]);

  const fetchOptions = useCallback(() => {
    apiGet(`/api/editor/${encodeURIComponent(jobId)}/export/options`, token)
      .then((d: ExportOptions) => {
        setOptions(d);
        setOptionsError(null);
        // Seed 1080p+balanced (not High — see ExportDialogBody's own note on
        // why High isn't the safe default), but only pick a fresh combo the
        // FIRST time options load, not on every re-fetch after a completed
        // encode (that would yank the user's own quality/resolution choice
        // back to the default right after they picked something else).
      })
      .catch((e) => setOptionsError(e instanceof Error ? e.message : String(e)));
  }, [jobId, token]);

  const openDialog = useCallback(() => {
    setDialogOpen(true);
    fetchOptions();
  }, [fetchOptions]);

  const closeDialog = useCallback(() => setDialogOpen(false), []);

  const startExport = useCallback(async () => {
    setExportState("requesting");
    setExportError(null);
    try {
      const result = await apiPost(`/api/editor/${encodeURIComponent(jobId)}/export`, token, { resolution, quality });
      if (result.state === "done") {
        setExportState("done");
        setFilename(result.filename);
        setBytes(result.bytes);
        setFitsWhatsapp(options ? result.bytes <= options.whatsapp_limit_bytes : null);
        fetchOptions();
        return;
      }
      setExportState("encoding");
      setProgress(0);
      clearPoll();
      pollRef.current = window.setInterval(async () => {
        try {
          const status = await apiGet(`/api/editor/${encodeURIComponent(jobId)}/export/status`, token);
          setProgress(status.progress ?? 0);
          if (status.state === "done" || status.state === "failed") {
            clearPoll();
            setExportState(status.state);
            if (status.state === "done") {
              setFilename(status.filename);
              setBytes(status.bytes);
              setFitsWhatsapp(status.fits_whatsapp);
              fetchOptions();
              onDone?.(`Exported ${status.filename}`);
            } else {
              setExportError(status.error || "Export failed");
            }
          }
        } catch {
          // A single failed poll isn't conclusive — the server may just be
          // momentarily unresponsive; try again on the next tick rather than
          // failing the whole export over one dropped request.
        }
      }, 1500);
    } catch (e) {
      const status = (e as { status?: number })?.status;
      setExportState("failed");
      setExportError(
        status === 429 ? "Hourly export limit reached — try again later."
          : status === 409 ? "A save is still rendering — export once it finishes so you get the new version."
          : String(e instanceof Error ? e.message : e)
      );
    }
  }, [jobId, token, resolution, quality, options, fetchOptions, clearPoll, onDone]);

  // Dirty-state UX: Export never silently transcodes a stale preview.
  // requestExport() is what the primary CTA calls — if the editor has
  // unsaved edits it triggers a save and REMEMBERS to auto-continue once
  // that save lands, rather than exporting the old file. exportAnyway() is
  // the explicit escape hatch for "I know, export the last saved version."
  const requestExport = useCallback(() => {
    if (isDirty) {
      setPendingAfterSave(true);
      onSave();
      return;
    }
    startExport();
  }, [isDirty, onSave, startExport]);

  const exportAnyway = useCallback(() => {
    startExport();
  }, [startExport]);

  useEffect(() => {
    if (!pendingAfterSave) return;
    if (saveState === "done") {
      setPendingAfterSave(false);
      startExport();
    } else if (saveState === "failed") {
      setPendingAfterSave(false);
    }
    // Deliberately NOT depending on startExport's identity churning this
    // effect — only the save outcome should trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingAfterSave, saveState]);

  return {
    dialogOpen, openDialog, closeDialog,
    options, optionsError,
    resolution, setResolution, quality, setQuality,
    exportState, progress, filename, bytes, fitsWhatsapp, exportError,
    pendingAfterSave,
    requestExport, exportAnyway,
    dismissError: () => setExportState("idle"),
  };
}

export type UseExportReturn = ReturnType<typeof useExport>;
