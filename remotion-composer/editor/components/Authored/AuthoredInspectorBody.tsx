import React from "react";
import { ObjectField } from "../../SchemaForm";
import { framesToTimecode } from "../../state/playhead";
import type { ManifestElement } from "../../state/authoredHitTest";
import type { CaptionChunk } from "../../state/authoredCaptions";
import { KIND_LABEL, schemaForElement, contentDefaults } from "../../state/authoredKindSchemas";
import { CutsPanel } from "../Inspector/CutsPanel";
import type { VideoCut } from "../../../src/cuts";

/**
 * The three-way Inspector content (selected manifest element / selected
 * caption chunk / scene settings) — extracted from AuthoredEditor.tsx
 * (Phase 5, phone shell) so the desktop `.inspector` sidebar and
 * AuthoredPhoneShell.tsx's "Edit" PhoneSheet render identical content in
 * different wrapping chrome. No state of its own; every callback is
 * something AuthoredEditorInner already owns.
 */
const CAPTION_BOX_ID = "__captionBox";

export function AuthoredInspectorBody({
  selectedEl,
  selectedCaptionChunk,
  selectedCaptionBox,
  captionBoxRect,
  effectiveValues,
  errorForField,
  onElementChange,
  onResetSelected,
  overrides,
  onCaptionTextEdit,
  onResetCaptions,
  fps,
  videoVolume,
  onVideoVolumeChange,
  onDeselect,
  cuts,
  sourceDurationFrames,
  onCutsChange,
}: {
  selectedEl: ManifestElement | null;
  selectedCaptionChunk: CaptionChunk | null;
  /** True when the caption BLOCK's own position/size box is selected —
   *  distinct from selectedCaptionChunk (a word-group's TIME window). */
  selectedCaptionBox: boolean;
  /** Effective (override-applied) rect for that box, purely for display —
   *  the actual drag/resize happens on the canvas overlay, not here. */
  captionBoxRect: { x: number; y: number; w: number; h: number };
  effectiveValues: Record<string, Record<string, unknown>>;
  errorForField: (id: string, field: string) => string | undefined;
  onElementChange: (id: string, patch: Record<string, unknown>, coalesceKey: string | null) => void;
  onResetSelected: () => void;
  overrides: Record<string, Record<string, unknown>>;
  onCaptionTextEdit: (chunkKey: number, newText: string) => void;
  onResetCaptions: () => void;
  fps: number;
  videoVolume: number;
  onVideoVolumeChange: (value: number) => void;
  onDeselect: () => void;
  cuts: VideoCut[];
  sourceDurationFrames: number;
  onCutsChange: (next: VideoCut[]) => void;
}) {
  if (selectedEl) {
    return (
      <>
        <div className="inspector__header">
          <div className="inspector__title">
            {selectedEl.id}
            <span className="toolbar__spacer" />
            <button type="button" className="btn btn--icon btn--ghost" onClick={onDeselect} title="Deselect (Esc)">✕</button>
          </div>
          <div className="inspector__kind">
            {KIND_LABEL[selectedEl.kind]}
            {selectedEl.recovered && (
              <span
                className="inspector__badge"
                title="This element wasn't listed in the scene's manifest, but its overrides are genuinely wired — recovered from the generated code. Position/size aren't recoverable this way, so those controls are hidden."
              >
                {" "}· recovered — no position/size
              </span>
            )}
          </div>
        </div>
        <div className="inspector__body">
          <ObjectField
            schema={schemaForElement(selectedEl)}
            value={effectiveValues[selectedEl.id] || contentDefaults(selectedEl)}
            errorFor={(field) => errorForField(selectedEl.id, field)}
            onChange={(next) => {
              const fieldNames = Object.keys(schemaForElement(selectedEl).properties || {});
              const patch: Record<string, unknown> = {};
              for (const name of fieldNames) patch[name] = (next as Record<string, unknown>)[name];
              onElementChange(selectedEl.id, patch, `inspector:${selectedEl.id}`);
            }}
          />
          <div className="inspector__actions">
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={onResetSelected}
              disabled={!(selectedEl.id in overrides)}
            >
              Reset to authored default
            </button>
          </div>
        </div>
      </>
    );
  }

  if (selectedCaptionChunk) {
    return (
      <>
        <div className="inspector__header">
          <div className="inspector__title">
            Caption
            <span className="toolbar__spacer" />
            <button type="button" className="btn btn--icon btn--ghost" onClick={onDeselect} title="Deselect (Esc)">✕</button>
          </div>
          <div className="inspector__kind">
            {framesToTimecode(Math.round(selectedCaptionChunk.fromSec * fps))} – {framesToTimecode(Math.round(selectedCaptionChunk.toSec * fps))}
          </div>
        </div>
        <div className="inspector__body">
          <div className="field">
            <label className="field__label">Text</label>
            <textarea
              className="input"
              value={selectedCaptionChunk.text}
              onChange={(e) => onCaptionTextEdit(selectedCaptionChunk.key, e.target.value)}
              rows={3}
            />
            <div className="field__hint">
              Chunk boundaries here are an estimate — the render may group these words slightly
              differently. Word text and timing you edit are exact; drag/trim on the timeline to retime.
            </div>
          </div>
          <div className="inspector__actions">
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={onResetCaptions}
              disabled={!("__captions" in overrides)}
              title="Discard every caption edit and revert to the originally transcribed text/timing"
            >
              Reset all captions to original
            </button>
          </div>
        </div>
      </>
    );
  }

  if (selectedCaptionBox) {
    return (
      <>
        <div className="inspector__header">
          <div className="inspector__title">
            Caption position
            <span className="toolbar__spacer" />
            <button type="button" className="btn btn--icon btn--ghost" onClick={onDeselect} title="Deselect (Esc)">✕</button>
          </div>
          <div className="inspector__kind">
            x {Math.round(captionBoxRect.x)}, y {Math.round(captionBoxRect.y)} · {Math.round(captionBoxRect.w)}×{Math.round(captionBoxRect.h)}
          </div>
        </div>
        <div className="inspector__body">
          <div className="field__hint">
            Drag this box in the preview to move where captions appear on screen, or use the
            corner handle to resize it. This controls the whole caption container — the timeline's
            Captions lane still controls when each phrase appears and what it says.
          </div>
          <div className="inspector__actions">
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={onResetSelected}
              disabled={!(CAPTION_BOX_ID in overrides)}
            >
              Reset to authored default
            </button>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="inspector__header">
        <div className="inspector__title">Scene settings</div>
      </div>
      <div className="inspector__body">
        <div className="field">
          <label className="field__label">Speaker volume</label>
          <div className="volume-row">
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={videoVolume}
              onChange={(e) => onVideoVolumeChange(Number(e.target.value))}
            />
            <span className="volume-row__value">{Math.round(videoVolume * 100)}%</span>
          </div>
          <div className="field__hint">Audible live in this preview.</div>
        </div>
        <div className="field">
          <label className="field__label">Cuts</label>
          <CutsPanel
            cuts={cuts}
            sourceDurationFrames={sourceDurationFrames}
            onCutsChange={onCutsChange}
          />
        </div>
      </div>
      <div className="inspector__empty">
        Click an element in the preview to edit its content, position, or size.
      </div>
    </>
  );
}
