import React from "react";
import type { ExportQuality, ExportResolution, UseExportReturn } from "../../state/useExport";

const RESOLUTION_LABEL: Record<ExportResolution, string> = {
  "1080p": "1080 × 1920",
  "720p": "720 × 1280",
};
const RESOLUTION_SUBLABEL: Record<ExportResolution, string> = {
  "1080p": "Full",
  "720p": "Smaller file",
};
const QUALITY_LABEL: Record<ExportQuality, string> = {
  high: "High",
  balanced: "Balanced",
  small: "Small",
};

/** MB = 1,000,000 bytes everywhere in this UI — matching the decimal 16 MB
 *  WhatsApp limit the server sends. Mixing in MiB (÷1024²) is how you end up
 *  displaying "15.9 MB fits" for a file that's actually over the real limit. */
function formatMB(bytes: number): string {
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

function relativeTime(epochSeconds: number): string {
  const diffS = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (diffS < 60) return "just now";
  if (diffS < 3600) return `${Math.round(diffS / 60)} min ago`;
  if (diffS < 86400) return `${Math.round(diffS / 3600)} hr ago`;
  return `${Math.round(diffS / 86400)} d ago`;
}

/** A real, right-clickable `<a download>` — never a JS click or a new tab
 *  (Content-Type video/mp4 plays inline, worse on mobile Safari). `?download=1`
 *  is what actually forces the save on iOS Safari, which ignores the `download`
 *  HTML attribute for a same-origin navigation (see serve_file's own comment). */
function DownloadLink({
  jobId, token, filename, resolution, quality,
}: { jobId: string; token: string; filename: string; resolution: ExportResolution; quality: ExportQuality }) {
  const href = `/files/${encodeURIComponent(jobId)}/${encodeURIComponent(filename)}?download=1&token=${encodeURIComponent(token)}`;
  return (
    <a className="btn btn--primary" href={href} download={`openmontage-${jobId}-${resolution}-${quality}.mp4`}>
      Download
    </a>
  );
}

/**
 * The entire Export dialog's content — no chrome of its own, so desktop's
 * ExportDialog (a position:fixed modal) and mobile's existing PhoneSheet can
 * both drop it in unmodified. Same Body/Shell split this codebase already
 * uses for AuthoredInspectorBody. All state comes from useExport(); this
 * component only reads it and calls its actions.
 */
export function ExportDialogBody({
  x, jobId, token, isDirty,
}: { x: UseExportReturn; jobId: string; token: string; isDirty: boolean }) {
  const {
    options, optionsError, resolution, setResolution, quality, setQuality,
    exportState, progress, filename, bytes, fitsWhatsapp, exportError,
    pendingAfterSave, requestExport, exportAnyway, dismissError,
  } = x;

  const busy = exportState === "requesting" || exportState === "encoding" || pendingAfterSave;

  if (optionsError) {
    return <div className="banner banner--error">Couldn't load export options: {optionsError}</div>;
  }
  if (!options) {
    return <div className="inspector__empty">Loading…</div>;
  }

  const selectedCombo = options.combos.find((c) => c.resolution === resolution && c.quality === quality) || null;
  const bestFittingCombo = options.combos
    .filter((c) => c.fits_whatsapp)
    .sort((a, b) => (a.cached ? (a.cached_bytes ?? a.estimated_bytes) : a.estimated_bytes)
      - (b.cached ? (b.cached_bytes ?? b.estimated_bytes) : b.estimated_bytes))
    .pop() || null;

  const selectedFits = selectedCombo?.fits_whatsapp ?? true;
  const selectedIsCached = selectedCombo?.cached ?? false;
  const selectedSizeBytes = selectedCombo
    ? (selectedCombo.cached ? (selectedCombo.cached_bytes ?? selectedCombo.estimated_bytes) : selectedCombo.estimated_bytes)
    : 0;

  const remaining = Math.max(0, options.exports_per_hour - options.exports_this_hour);
  const outOfExports = remaining <= 0;

  // A just-finished export (exportState === "done") always wins over the
  // "this combo happens to already be cached" read — filename/bytes there
  // are the server's own confirmation of what actually got written, not a
  // re-derivation from the options list (which may not have refetched yet).
  const readyFilename = exportState === "done" ? filename : (selectedIsCached ? selectedCombo?.filename ?? null : null);
  const readyBytes = exportState === "done" ? bytes : (selectedIsCached ? selectedSizeBytes : null);
  const readyFits = exportState === "done" ? fitsWhatsapp : (selectedIsCached ? selectedFits : null);

  return (
    <div className="export-dialog">
      {isDirty && !busy && (
        <div className="banner banner--warn">
          You have unsaved edits. Export uses your last saved video
          (rendered {relativeTime(options.source.modified_at)}) — your on-screen changes are not in it.
        </div>
      )}

      {options.save_state === "rendering" && (
        <div className="banner banner--warn">A save is still rendering — export once it finishes so you get the new version.</div>
      )}

      <div>
        <div className="field__label">Resolution</div>
        <div className="exportgrid exportgrid--resolution">
          {(["1080p", "720p"] as ExportResolution[]).map((r) => (
            <button
              key={r}
              type="button"
              className="exportgrid__cell"
              aria-pressed={resolution === r}
              disabled={busy}
              onClick={() => setResolution(r)}
            >
              <span className="exportgrid__label">{RESOLUTION_LABEL[r]}</span>
              <span className="exportgrid__size">{RESOLUTION_SUBLABEL[r]}</span>
            </button>
          ))}
        </div>
        <div className="field__hint">720p is the same video, scaled down — nothing is re-laid-out.</div>
      </div>

      <div>
        <div className="field__label">File size</div>
        <div className="exportgrid exportgrid--quality">
          {(["high", "balanced", "small"] as ExportQuality[]).map((q) => {
            const combo = options.combos.find((c) => c.resolution === resolution && c.quality === q);
            const size = combo ? (combo.cached ? (combo.cached_bytes ?? combo.estimated_bytes) : combo.estimated_bytes) : 0;
            const fits = combo?.fits_whatsapp ?? true;
            return (
              <button
                key={q}
                type="button"
                className={`exportgrid__cell${!fits ? " exportgrid__cell--toobig" : ""}`}
                aria-pressed={quality === q}
                disabled={busy}
                onClick={() => setQuality(q)}
              >
                <span className="exportgrid__label">{QUALITY_LABEL[q]}</span>
                <span className="exportgrid__size">{combo ? (combo.cached ? `${formatMB(size)} (ready)` : `≈ ${formatMB(size)}`) : "…"}</span>
                <span className={`exportgrid__chip${fits ? "" : " exportgrid__chip--warn"}`}>
                  {fits ? "fits WhatsApp" : "too big for WhatsApp"}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="export-summary" title="Everything is mastered at 30 fps.">
        {selectedCombo ? (
          <>
            {selectedCombo.width} × {selectedCombo.height} · 30 fps ·{" "}
            {selectedIsCached ? formatMB(selectedSizeBytes) : `≈ ${formatMB(selectedSizeBytes)}`} ·{" "}
            fits WhatsApp {selectedFits ? "✓" : "✗"}
          </>
        ) : "…"}
      </div>

      {!selectedFits && bestFittingCombo && (
        <div className="banner banner--warn">
          This combination is over WhatsApp's 16 MB attachment limit — WhatsApp won't let you attach
          files that large. You can still download it directly.{" "}
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => { setResolution(bestFittingCombo.resolution); setQuality(bestFittingCombo.quality); }}
          >
            Use {RESOLUTION_LABEL[bestFittingCombo.resolution]} · {QUALITY_LABEL[bestFittingCombo.quality]} instead
          </button>
        </div>
      )}

      {(exportState === "requesting" || exportState === "encoding" || pendingAfterSave) && (
        <div>
          <div className="progressbar">
            <div className="progressbar__fill" style={{ width: `${pendingAfterSave ? 0 : progress}%` }} />
          </div>
          <div className="field__hint">
            {pendingAfterSave ? "Saving your edits… then exporting" : `Encoding… ${progress}%`}
          </div>
        </div>
      )}

      {exportState === "failed" && exportError && (
        <div className="banner banner--error">
          {exportError}{" "}
          <button type="button" className="btn btn--sm btn--ghost" onClick={dismissError}>Dismiss</button>
        </div>
      )}

      {readyFilename && readyBytes !== null && (
        <div className="banner banner--ok">
          Ready — {formatMB(readyBytes)} {readyFits === false ? "(over WhatsApp's limit, download only)" : ""}
        </div>
      )}

      <div className="modal__footer" style={{ padding: 0, border: "none", marginTop: 4 }}>
        {isDirty ? (
          <>
            <button type="button" className="btn" onClick={exportAnyway} disabled={busy}>
              Export last saved anyway
            </button>
            <button type="button" className="btn btn--primary" onClick={requestExport} disabled={busy || outOfExports}>
              {pendingAfterSave ? "Saving…" : "Save, then export"}
            </button>
          </>
        ) : readyFilename ? (
          <DownloadLink jobId={jobId} token={token} filename={readyFilename} resolution={resolution} quality={quality} />
        ) : (
          <button
            type="button"
            className="btn btn--primary"
            onClick={requestExport}
            disabled={busy || outOfExports}
            title={outOfExports ? "Hourly export limit reached — try again later" : undefined}
          >
            {busy ? "Working…" : "Export"}
          </button>
        )}
      </div>

      <div className="field__hint">{remaining}/{options.exports_per_hour} exports left this hour</div>
    </div>
  );
}
