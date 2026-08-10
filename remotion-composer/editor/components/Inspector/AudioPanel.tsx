import React from "react";

/**
 * The two volume sliders — extracted out of ProjectInspector so the phone
 * shell's dedicated "Audio" sheet (Phase 6, F2b) can render exactly this
 * and nothing else, instead of the whole no-selection project panel. Same
 * component either way — one source of truth for the slider markup/defaults.
 */
export function AudioPanel({
  props,
  onChange,
}: {
  props: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
}) {
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
          Applies to the delivered video only — the music bed is mixed in after render, so it is NOT audible in this preview.
        </div>
      </div>
    </>
  );
}
