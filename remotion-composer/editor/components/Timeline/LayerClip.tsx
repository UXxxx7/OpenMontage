import React from "react";
import type { LayerClipItem } from "../../state/layers";

/**
 * One clip in the Timeline's "Layers" view — visually similar to Clip.tsx
 * but deliberately NOT the same component: no resize handles (this view
 * doesn't edit time, only stacking order), no packRows sub-row offset
 * (overlap within one layer row is expected here — that's the whole point
 * of z-order), and its pointer handlers come from useLayerDrag (vertical),
 * not useClipDrag (horizontal).
 */
export const LayerClip = React.memo(function LayerClip({
  clip,
  pxPerFrame,
  selected,
  revealed,
  color,
  dragEnabled,
  onPointerDown,
  dragHandlers,
}: {
  clip: LayerClipItem;
  pxPerFrame: number;
  selected: boolean;
  /** See Clip.tsx's own doc on this prop — same reveal-pulse mechanism. */
  revealed: boolean;
  color: string;
  dragEnabled: boolean;
  onPointerDown: (clip: LayerClipItem) => (e: React.PointerEvent<HTMLElement>) => void;
  dragHandlers: {
    onPointerMove: (e: React.PointerEvent<HTMLElement>) => void;
    onPointerUp: (e: React.PointerEvent<HTMLElement>) => void;
    onPointerCancel: (e: React.PointerEvent<HTMLElement>) => void;
  };
}) {
  if (clip.isPoint) {
    return (
      <div
        className={`marker${selected ? " marker--selected" : ""}${revealed ? " marker--revealed" : ""}`}
        style={{ left: clip.from * pxPerFrame, top: 6, background: color }}
        title={`${clip.label} @ frame ${clip.from} — layer ${clip.layer}`}
        onPointerDown={onPointerDown(clip)}
        {...dragHandlers}
      />
    );
  }

  const width = Math.max(3, (clip.to - clip.from) * pxPerFrame);

  return (
    <div
      className={
        "clip" +
        (selected ? " clip--selected" : "") +
        (clip.openEnded ? " clip--openended" : "") +
        (revealed ? " clip--revealed" : "")
      }
      style={{
        left: clip.from * pxPerFrame,
        width,
        top: 2,
        background: color,
        cursor: dragEnabled ? "ns-resize" : undefined,
      }}
      title={`${clip.label} — layer ${clip.layer}${dragEnabled ? " (drag up/down to change stacking)" : ""}`}
      onPointerDown={onPointerDown(clip)}
      {...dragHandlers}
    >
      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{clip.label}</span>
      {clip.openEnded && <span className="clip__inf" title="No end frame — runs to the end of the video">∞</span>}
    </div>
  );
});
