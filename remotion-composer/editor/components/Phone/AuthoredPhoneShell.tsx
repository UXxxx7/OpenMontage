import React, { useRef, useState } from "react";
import type { PlayerRef } from "@remotion/player";
import { useResizablePanel } from "../../state/useResizablePanel";
import type { ManifestElement } from "../../state/authoredHitTest";
import type { CaptionChunk, Word } from "../../state/authoredCaptions";
import type { AuthoredValidationResult } from "../../state/authoredValidation";
import type { VideoCut } from "../../../src/cuts";
import { AuthoredPreview } from "../Authored/AuthoredPreview";
import { AuthoredInspectorBody } from "../Authored/AuthoredInspectorBody";
import { AuthoredTimeline } from "../Authored/AuthoredTimeline";
import { PhoneTopBar } from "./PhoneTopBar";
import { PhoneActionBar, type PhoneActionItem, type PhoneSheetKind } from "./PhoneActionBar";
import { PhoneSheet } from "./PhoneSheet";
import type { SaveState } from "../Toolbar";

const ARM_B_SHEET_ITEMS: PhoneActionItem[] = [
  { kind: "edit", icon: "🎛", label: "Edit" },
];

/**
 * Phone-first shell for Arm B (Phase 5 of the "Arm B editor parity" work) —
 * AuthoredEditor.tsx returned before Arm A's own isPhone branch entirely
 * (App.tsx:147 vs :539), so this arm was desktop-grid-only until now.
 *
 * Deliberately NOT a reuse of Arm A's PhoneShell.tsx: ~28 of its 40 props
 * are Arm A-only (schema/render_props shapes, cuts, add/duplicate/delete,
 * onRelayout, a `lanes`/`layerRows` shape this arm doesn't produce). What
 * genuinely generalizes — PhoneSheet (already generic), PhoneTopBar
 * (Phase 5 made onRelayout/quota optional), PhoneActionBar (Phase 5 made
 * its sheet list a prop instead of a hardcoded 4), useResizablePanel, and
 * the `.phone-app*` CSS classes — is reused directly. The preview
 * (AuthoredPreview) and Inspector content (AuthoredInspectorBody) are
 * ALSO shared with the desktop layout (both extracted from
 * AuthoredEditor.tsx in this same pass), so this shell only differs from
 * desktop in chrome, exactly like Arm A's PhoneShell does relative to
 * App.tsx's Editor.
 *
 * Arm B's action bar has just ONE sheet ("Edit" — manifest element,
 * caption, or scene settings, whichever is selected) plus the Layers
 * toggle: no Add (can't add model-authored elements), no Cut (no cuts
 * model), no separate Audio sheet (the volume slider already lives inside
 * "Edit"'s scene-settings fallback, same as desktop).
 *
 * All state stays owned by AuthoredEditorInner — this receives it as
 * props and calls back into it, the same contract Arm A's PhoneShell has
 * with App.tsx's Editor. A JSX split, not a state refactor.
 */
export function AuthoredPhoneShell({
  jobId,
  jobStatus,
  isDirty,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onSave,
  saveState,
  saveError,
  onDismissSaveError,
  busy,
  validation,
  scenePredatesPositionEditing,

  component,
  inputProps,
  durationInFrames,
  outputDurationInFrames,
  fps,
  width,
  height,
  playerRef,
  manifest,
  overrides,
  selectedId,
  onSelect,
  onOverlayCommit,
  previewUrl,
  compileError,

  filmstripUrls,
  waveformUrl,
  effectiveWords,
  onCaptionRetime,
  onSeek,
  onScrubStart,
  onScrubEnd,
  hasItemError,
  onElementTimeCommit,

  selectedEl,
  selectedCaptionChunk,
  selectedCaptionBox,
  captionBoxRect,
  effectiveValues,
  errorForField,
  onElementChange,
  onResetSelected,
  onCaptionTextEdit,
  onResetCaptions,
  videoVolume,
  onVideoVolumeChange,
  cuts,
  onCutsChange,
}: {
  jobId: string;
  jobStatus: string;
  isDirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onSave: () => void;
  saveState: SaveState;
  saveError: string | null;
  onDismissSaveError: () => void;
  busy: boolean;
  validation: AuthoredValidationResult;
  scenePredatesPositionEditing: boolean;

  component: React.ComponentType<Record<string, unknown>> | null;
  inputProps: Record<string, unknown>;
  /** SOURCE-frame duration — feeds AuthoredPreview, which converts to
   *  OUTPUT internally (Phase 3). */
  durationInFrames: number;
  /** OUTPUT-frame duration (post-cuts) — feeds AuthoredTimeline directly,
   *  computed once in AuthoredEditor.tsx (single source of truth, same
   *  reasoning as Arm A's App.tsx durationInFrames/sourceDurationFrames split). */
  outputDurationInFrames: number;
  fps: number;
  width: number;
  height: number;
  playerRef: React.RefObject<PlayerRef>;
  manifest: ManifestElement[];
  overrides: Record<string, Record<string, unknown>>;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onOverlayCommit: (id: string, patch: { x?: number; y?: number; w?: number; h?: number }) => void;
  previewUrl: string | null;
  compileError: string | null;

  filmstripUrls: string[];
  waveformUrl: string | null;
  effectiveWords: Word[];
  onCaptionRetime: (chunkKey: number, fromFrame: number, toFrame: number) => void;
  onSeek: (frame: number) => void;
  onScrubStart: () => void;
  onScrubEnd: () => void;
  hasItemError: (id: string) => boolean;
  onElementTimeCommit: (id: string, patch: { mountFrame?: number; endFrame?: number }) => void;

  selectedEl: ManifestElement | null;
  selectedCaptionChunk: CaptionChunk | null;
  selectedCaptionBox: boolean;
  captionBoxRect: { x: number; y: number; w: number; h: number };
  effectiveValues: Record<string, Record<string, unknown>>;
  errorForField: (id: string, field: string) => string | undefined;
  onElementChange: (id: string, patch: Record<string, unknown>, coalesceKey: string | null) => void;
  onResetSelected: () => void;
  onCaptionTextEdit: (chunkKey: number, newText: string) => void;
  onResetCaptions: () => void;
  videoVolume: number;
  onVideoVolumeChange: (value: number) => void;
  cuts: VideoCut[];
  onCutsChange: (next: VideoCut[]) => void;
}) {
  const [activeSheet, setActiveSheet] = useState<PhoneSheetKind>(null);
  const [timelineViewMode, setTimelineViewMode] = useState<"type" | "layers">("type");

  const rootRef = useRef<HTMLDivElement>(null);
  const { handleProps: previewResizeHandleProps } = useResizablePanel({
    storageKey: "om-editor-armb-phone-preview-h",
    cssVar: "--phone-preview-h",
    targetRef: rootRef,
    defaultPx: () => window.innerHeight * 0.4,
    minPx: 120,
    maxPx: () => Math.max(160, window.innerHeight * 0.75),
    growsWhenDraggedUp: false,
  });

  const openSheet = (kind: Exclude<PhoneSheetKind, null>) => {
    setActiveSheet((cur) => (cur === kind ? null : kind));
  };

  // Same auto-open-Edit-on-select behavior as Arm A's PhoneShell, and the
  // same reason: only on an actual selection CHANGE, or the pointerdown
  // that starts a touch-drag (useClipDrag.ts calls onSelect unconditionally
  // before checking whether the drag is even allowed) would re-open the
  // sheet mid-gesture and cover the timeline the drag needs to stay on.
  const handleSelect = (id: string | null) => {
    const changed = id !== selectedId;
    onSelect(id);
    if (id && changed && activeSheet === null) setActiveSheet("edit");
  };

  return (
    <div className="phone-app" ref={rootRef}>
      <PhoneTopBar
        jobId={jobId}
        jobStatus={jobStatus}
        isDirty={isDirty}
        canUndo={canUndo}
        canRedo={canRedo}
        onUndo={onUndo}
        onRedo={onRedo}
        onSave={onSave}
        saveState={saveState}
      />

      {!validation.valid && !busy && (
        <div className="banner banner--warn" style={{ position: "fixed", bottom: 12, left: "50%", transform: "translateX(-50%)", zIndex: 50, maxWidth: 520 }}>
          {validation.errors.length} validation {validation.errors.length === 1 ? "issue" : "issues"} — fix the highlighted fields before saving.
        </div>
      )}

      {scenePredatesPositionEditing && (
        <div
          className="banner banner--warn"
          style={{ position: "fixed", top: `calc(var(--toolbar-h) + 10px)`, left: "50%", transform: "translateX(-50%)", zIndex: 50, maxWidth: 520 }}
        >
          This scene predates live position/size editing — dragging elements here won't move them in the render. Ask for a revision (or send a fresh video) to regenerate it.
        </div>
      )}

      {saveState === "failed" && saveError && (
        <div className="toast">
          {saveError}
          <button type="button" className="btn btn--sm btn--ghost" onClick={onDismissSaveError}>✕</button>
        </div>
      )}

      <div className="phone-app__preview">
        <AuthoredPreview
          component={component}
          inputProps={inputProps}
          durationInFrames={durationInFrames}
          fps={fps}
          width={width}
          height={height}
          playerRef={playerRef}
          manifest={manifest}
          overrides={overrides}
          selectedId={selectedId}
          onSelect={handleSelect}
          onCommit={onOverlayCommit}
          previewUrl={previewUrl}
          compileError={compileError}
        />
      </div>

      <div
        className="phone-app__resize-h"
        title="Drag to resize the preview"
        {...previewResizeHandleProps}
      >
        <span className="phone-app__resize-grip" />
      </div>

      <div className="phone-app__timeline">
        <AuthoredTimeline
          manifest={manifest}
          overrides={overrides}
          durationInFrames={outputDurationInFrames}
          cuts={cuts}
          selectedId={selectedId}
          onSelect={handleSelect}
          onCommit={onElementTimeCommit}
          onCaptionRetime={onCaptionRetime}
          onSeek={onSeek}
          hasItemError={hasItemError}
          isTouch
          filmstripUrls={filmstripUrls}
          waveformUrl={waveformUrl}
          words={effectiveWords}
          fps={fps}
          onScrubStart={onScrubStart}
          onScrubEnd={onScrubEnd}
        />
      </div>

      <PhoneActionBar
        items={ARM_B_SHEET_ITEMS}
        activeSheet={activeSheet}
        onOpenSheet={openSheet}
        layersToggle={{
          active: timelineViewMode === "layers",
          onToggle: () => setTimelineViewMode((m) => (m === "layers" ? "type" : "layers")),
          title: "Group by stacking order (read-only — this scene's generated code doesn't wire layer edits to the render yet)",
        }}
      />

      {activeSheet === "edit" && (
        <PhoneSheet title="Edit" onClose={() => setActiveSheet(null)}>
          <AuthoredInspectorBody
            selectedEl={selectedEl}
            selectedCaptionChunk={selectedCaptionChunk}
            selectedCaptionBox={selectedCaptionBox}
            captionBoxRect={captionBoxRect}
            effectiveValues={effectiveValues}
            errorForField={errorForField}
            onElementChange={onElementChange}
            onResetSelected={onResetSelected}
            overrides={overrides}
            onCaptionTextEdit={onCaptionTextEdit}
            onResetCaptions={onResetCaptions}
            fps={fps}
            videoVolume={videoVolume}
            onVideoVolumeChange={onVideoVolumeChange}
            onDeselect={() => onSelect(null)}
            cuts={cuts}
            sourceDurationFrames={durationInFrames}
            onCutsChange={onCutsChange}
          />
        </PhoneSheet>
      )}
    </div>
  );
}
