import React, { useRef, useState } from "react";

type UploadState = "idle" | "uploading" | "failed";

/**
 * The two volume sliders — extracted out of ProjectInspector so the phone
 * shell's dedicated "Audio" sheet (Phase 6, F2b) can render exactly this
 * and nothing else, instead of the whole no-selection project panel. Same
 * component either way — one source of truth for the slider markup/defaults.
 *
 * `jobId`/`token` were added for the "upload your own background music"
 * control below the sliders — POST/DELETE straight to the editor's music
 * routes (not through api.ts's apiPost, which is JSON-only; this needs a
 * real multipart body). On a successful upload, `musicSrc` is set via the
 * normal `onChange` so it's live in the editor Player immediately, same as
 * any other prop edit — the server re-derives/pins the real value from disk
 * on save regardless of what's sent (see pipeline_runner.pin_music_src_prop),
 * so trusting the client's own upload response here for LIVE PREVIEW purposes
 * is not a security boundary the way a save is.
 */
export function AudioPanel({
  props,
  onChange,
  jobId,
  token,
}: {
  props: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
  jobId: string;
  token: string;
}) {
  const [uploadState, setUploadState] = useState<UploadState>("idle");
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const musicSrc = typeof props.musicSrc === "string" ? props.musicSrc : null;
  const musicUrl = `/api/editor/${encodeURIComponent(jobId)}/music?token=${encodeURIComponent(token)}`;

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // clear so re-selecting the same file still fires onChange
    if (!file) return;
    setUploadState("uploading");
    setUploadError(null);
    try {
      const body = new FormData();
      body.append("file", file);
      const resp = await fetch(musicUrl, { method: "POST", body });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
      onChange("musicSrc", data.music_url);
      setUploadState("idle");
    } catch (err) {
      setUploadState("failed");
      setUploadError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleRemove = async () => {
    setUploadState("uploading");
    try {
      await fetch(musicUrl, { method: "DELETE" });
    } catch {
      // Best-effort — even if the DELETE itself fails to reach the server,
      // clearing musicSrc locally still stops using it in this editor
      // session; the next save's pin_music_src_prop reconciles against
      // whatever's really left on disk either way.
    }
    onChange("musicSrc", undefined);
    setUploadState("idle");
  };

  return (
    <>
      <div className="field">
        <label className="field__label">Speaker volume</label>
        <div className="volume-row">
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={typeof props.videoVolume === "number" ? props.videoVolume : 1}
            onChange={(e) => onChange("videoVolume", Number(e.target.value))}
          />
          <span className="volume-row__value">
            {Math.round((typeof props.videoVolume === "number" ? props.videoVolume : 1) * 100)}%
          </span>
        </div>
        <div className="field__hint">Audible live in this preview.</div>
      </div>

      <div className="field">
        <label className="field__label">Background music track</label>
        {musicSrc ? (
          <div className="audio-upload">
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <audio controls src={musicSrc} className="audio-upload__player" />
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={handleRemove}
              disabled={uploadState === "uploading"}
            >
              Remove
            </button>
          </div>
        ) : (
          <div className="audio-upload">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadState === "uploading"}
            >
              {uploadState === "uploading" ? "Uploading…" : "Upload track"}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/mp4,audio/x-m4a,audio/aac,audio/ogg"
              style={{ display: "none" }}
              onChange={handleFileChange}
            />
          </div>
        )}
        {uploadState === "failed" && uploadError && <div className="field__error">{uploadError}</div>}
        <div className="field__hint">
          {musicSrc
            ? "Your own track — replaces the AI-picked music bed and is audible live in this preview."
            : "MP3, WAV, M4A, AAC, or OGG, up to 20 MB. Replaces the AI-planned music bed once uploaded."}
        </div>
      </div>

      <div className="field">
        <label className="field__label">Background music volume</label>
        <div className="volume-row">
          <input
            type="range"
            min={0}
            max={0.4}
            step={0.02}
            value={typeof props.musicVolume === "number" ? props.musicVolume : 0.18}
            onChange={(e) => onChange("musicVolume", Number(e.target.value))}
          />
          <span className="volume-row__value">
            {Math.round((typeof props.musicVolume === "number" ? props.musicVolume : 0.18) * 100)}%
          </span>
        </div>
        <div className="field__hint">
          {musicSrc
            ? "Audible live in this preview — applies to the track uploaded above."
            : "Applies to the AI-planned music bed, mixed in after render — NOT audible in this preview unless you upload your own track above."}
        </div>
      </div>
    </>
  );
}
