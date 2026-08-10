import React, { useRef, useState } from "react";
import { useResizablePanel } from "../../state/useResizablePanel";
import type { PlayerRef } from "@remotion/player";
import type { XiaojinEditorialProps } from "../../../src/XiaojinEditorial";
import type { JSONSchema } from "../../SchemaForm";
import type { ClipItem, Lane } from "../../state/model";
import { itemAt } from "../../state/model";
import type { LayerClipItem, LayerRow } from "../../state/layers";
import type { VideoCut } from "../../../src/cuts";
import type { SaveState } from "../Toolbar";
import { PreviewPane } from "../PreviewPane";
import { LibraryPanel } from "../Library/LibraryPanel";
import { Timeline, type Selection } from "../Timeline/Timeline";
import { CutsPanel } from "../Inspector/CutsPanel";
import { ClipInspector } from "../Inspector/ClipInspector";
import { AudioPanel } from "../Inspector/AudioPanel";
import { PhoneTopBar } from "./PhoneTopBar";
import { PhoneActionBar, ARM_A_SHEET_ITEMS, type PhoneSheetKind } from "./PhoneActionBar";
import { PhoneSheet } from "./PhoneSheet";

/**
 * Phone-first shell (Phase 6, F2) — a dedicated portrait layout, not a
 * squeezed-down desktop one. Composes the SAME components the desktop tree
 * uses (PreviewPane, Timeline, LibraryPanel, CutsPanel, ClipInspector,
 * AudioPanel) with different chrome around them; none of the underlying
 * editing logic is duplicated or forked. All state stays owned by App.tsx's
 * Editor component — this receives it as props, same contract the desktop
 * tree already has.
 *
 * Replaces the old mobiletabs system entirely (three tabs stacking three
 * full panels in one grid cell was a confirmed, live bug — the Library
 * panel painted underneath the opaque Inspector/Timeline and was
 * completely invisible). Sheets here are mutually exclusive by construction
 * (one `activeSheet` value), so that class of bug can't recur.
 */
export function PhoneShell({
  jobId,
  jobStatus,
  schema,
  props,
  debouncedProps,
  selection,
  onSelect,
  onFieldChange,
  onItemChange,
  onAddItem,
  onDuplicateItem,
  onDeleteItem,
  errorForItem,
  cuts,
  sourceDurationFrames,
  onCutsChange,
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
  playerRef,
  onPlayingChange,
  lanes,
  layerRows,
  durationInFrames,
  onSeek,
  onTimeEdit,
  onLayerEdit,
  hasItemError,
  filmstripUrls,
  waveformUrl,
}: {
  jobId: string;
  jobStatus: string;
  schema: JSONSchema;
  props: Record<string, unknown>;
  debouncedProps: Record<string, unknown>;
  selection: Selection;
  onSelect: (sel: Selection) => void;
  onFieldChange: (name: string, value: unknown) => void;
  onItemChange: (section: string, index: number | null, next: Record<string, unknown>) => void;
  onAddItem: (section: string) => void;
  onDuplicateItem: (section: string, index: number) => void;
  onDeleteItem: (section: string, index: number) => void;
  errorForItem: (section: string, index: number | null) => (field: string) => string | undefined;
  cuts: VideoCut[];
  sourceDurationFrames: number;
  onCutsChange: (next: VideoCut[]) => void;
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
  playerRef: React.RefObject<PlayerRef | null>;
  onPlayingChange: (playing: boolean) => void;
  lanes: Lane[];
  layerRows: LayerRow[];
  durationInFrames: number;
  onSeek: (frame: number) => void;
  onTimeEdit: (clip: ClipItem, edit: { from?: number; to?: number }) => void;
  onLayerEdit: (clip: LayerClipItem, newLayer: number) => void;
  hasItemError: (section: string, index: number) => boolean;
  filmstripUrls: string[];
  waveformUrl: string | null;
}) {
  const [activeSheet, setActiveSheet] = useState<PhoneSheetKind>(null);
  const [timelineViewMode, setTimelineViewMode] = useState<"type" | "layers">("type");

  // Pause/resume playback around a playhead scrub — same reasoning and
  // pattern as App.tsx's own handleScrubStart/handleScrubEnd for the
  // desktop Timeline; kept local here (rather than threaded through as two
  // more props) since this component already receives `playerRef` directly.
  const wasPlayingBeforeScrubRef = useRef(false);
  const handleScrubStart = () => {
    wasPlayingBeforeScrubRef.current = playerRef.current?.isPlaying() ?? false;
    playerRef.current?.pause();
  };
  const handleScrubEnd = () => {
    if (wasPlayingBeforeScrubRef.current) playerRef.current?.play();
  };

  // Drag-to-resize the preview against the timeline below it (Phase 7) —
  // same hook desktop's App.tsx uses for its own preview/timeline
  // boundary, just controlling the PREVIEW's size here instead of the
  // TIMELINE's: .phone-app__timeline is `flex: 1 1 auto` (fills whatever's
  // left), so shrinking/growing the preview's own flex-basis is sufficient
  // to resize both at once without the timeline needing its own variable.
  const rootRef = useRef<HTMLDivElement>(null);
  const { handleProps: previewResizeHandleProps } = useResizablePanel({
    storageKey: "om-editor-phone-preview-h",
    cssVar: "--phone-preview-h",
    targetRef: rootRef,
    // Matches the shell's pre-Phase-7 fixed `40dvh` — only used for a
    // first-time visitor with nothing in localStorage yet.
    defaultPx: () => window.innerHeight * 0.4,
    minPx: 120,
    // Leaves at least 25% of the viewport for the timeline + action bar below.
    maxPx: () => Math.max(160, window.innerHeight * 0.75),
    // The controlled panel (preview) sits ABOVE this handle, not below —
    // dragging up must SHRINK it (growing the timeline via its own
    // flex: 1), the opposite of desktop's timeline-height handle.
    growsWhenDraggedUp: false,
  });

  // index===null selects the top-level OBJECT's own schema; a real index
  // selects that array's item schema — same split as Inspector.tsx's.
  const selectedItemSchema = selection
    ? selection.index === null
      ? (schema.properties?.[selection.section] as JSONSchema | undefined)
      : (schema.properties?.[selection.section] as JSONSchema | undefined)?.items
    : undefined;
  const selectedItem = selection ? itemAt(props, selection.section, selection.index) : undefined;

  const openSheet = (kind: Exclude<PhoneSheetKind, null>) => {
    setActiveSheet((cur) => (cur === kind ? null : kind));
  };

  // Selecting a clip (from the video or the timeline) is the natural moment
  // to jump straight to editing it — matches why the desktop Inspector
  // switches to ClipInspector automatically on selection. Only auto-opens
  // when nothing else is already open, so it never yanks the user out of
  // Add/Cut/Audio mid-task.
  //
  // Only on an actual CHANGE of selection, though (Phase 6, F3a) — every
  // touch-drag on an already-selected clip/layer-row STARTS with a
  // pointerdown that re-fires onSelect with the same target (useClipDrag.ts/
  // useLayerDrag.ts both call onSelect unconditionally, before checking
  // whether a drag is even allowed). Re-opening Edit on every one of those
  // would cover the timeline with the sheet before the drag gesture that
  // follows the tap ever got a chance to happen — the selected-only drag
  // rule needs the timeline to stay visible and reachable once selected.
  const handleSelect = (sel: Selection) => {
    const isSameSelection =
      (sel === null && selection === null) ||
      (!!sel && !!selection && sel.section === selection.section && sel.index === selection.index);
    onSelect(sel);
    if (sel && !isSameSelection && activeSheet === null) setActiveSheet("edit");
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
        onRelayout={onRelayout}
        saveState={saveState}
        relayoutBusy={relayoutBusy}
        savesThisHour={savesThisHour}
        savesPerHour={savesPerHour}
        slotBusy={slotBusy}
      />

      <div className="phone-app__preview">
        <PreviewPane
          props={debouncedProps as unknown as XiaojinEditorialProps}
          playerRef={playerRef}
          onPlayingChange={onPlayingChange}
          onItemChange={onItemChange}
          onSelect={handleSelect}
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
        <Timeline
          lanes={lanes}
          layerRows={layerRows}
          durationInFrames={durationInFrames}
          selection={selection}
          onSelect={handleSelect}
          onSeek={onSeek}
          onTimeEdit={onTimeEdit}
          onLayerEdit={onLayerEdit}
          hasItemError={hasItemError}
          isTouch
          filmstripUrls={filmstripUrls}
          waveformUrl={waveformUrl}
          cuts={cuts}
          viewMode={timelineViewMode}
          onViewModeChange={setTimelineViewMode}
          onScrubStart={handleScrubStart}
          onScrubEnd={handleScrubEnd}
        />
      </div>

      <PhoneActionBar
        items={ARM_A_SHEET_ITEMS}
        activeSheet={activeSheet}
        onOpenSheet={openSheet}
        layersToggle={{
          active: timelineViewMode === "layers",
          onToggle: () => setTimelineViewMode((m) => (m === "layers" ? "type" : "layers")),
          title: "Group timeline clips by stacking order — drag a clip up/down to change what draws on top",
        }}
      />

      {activeSheet === "add" && (
        <PhoneSheet title="Add to timeline" onClose={() => setActiveSheet(null)}>
          <LibraryPanel
            hideHeader
            isTouch
            onAdd={(section) => { onAddItem(section); setActiveSheet("edit"); }}
          />
        </PhoneSheet>
      )}

      {activeSheet === "cut" && (
        <PhoneSheet title="Cuts" onClose={() => setActiveSheet(null)}>
          <CutsPanel cuts={cuts} sourceDurationFrames={sourceDurationFrames} onCutsChange={onCutsChange} />
        </PhoneSheet>
      )}

      {activeSheet === "edit" && (
        <PhoneSheet title="Edit" onClose={() => setActiveSheet(null)}>
          {selection && (selectedItem || !selectedItemSchema) ? (
            <ClipInspector
              section={selection.section}
              index={selection.index}
              item={selectedItem ?? {}}
              itemSchema={selectedItemSchema}
              onChange={(next) => onItemChange(selection.section, selection.index, next)}
              onDuplicate={() => { if (selection.index !== null) onDuplicateItem(selection.section, selection.index); }}
              onDelete={() => {
                if (selection.index !== null) onDeleteItem(selection.section, selection.index);
                setActiveSheet(null);
              }}
              onClose={() => onSelect(null)}
              errorFor={errorForItem(selection.section, selection.index)}
            />
          ) : (
            <div className="inspector__empty">Tap a card on the video or timeline to edit it.</div>
          )}
        </PhoneSheet>
      )}

      {activeSheet === "audio" && (
        <PhoneSheet title="Audio" onClose={() => setActiveSheet(null)}>
          <AudioPanel props={props} onChange={onFieldChange} />
        </PhoneSheet>
      )}
    </div>
  );
}
