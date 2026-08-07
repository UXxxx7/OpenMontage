import React, { useRef } from "react";
import type { ClipItem } from "../../state/model";
import type { DragMode } from "./useClipDrag";

/**
 * 一个 clip。React.memo 到 props 级别——一条时间轴上可能有 40+ 个，拖动其中
 * 一个时不能把其它 39 个也重渲染一遍。
 */
export const Clip = React.memo(function Clip({
  clip,
  pxPerFrame,
  rowHeight,
  selected,
  invalid,
  revealed,
  color,
  resizable,
  dragEnabled,
  onPointerDown,
  dragHandlers,
}: {
  clip: ClipItem;
  pxPerFrame: number;
  rowHeight: number;
  selected: boolean;
  invalid: boolean;
  /** Brief highlight pulse right after this clip is revealed by an external
   *  selection (clicking the matching card on the video preview) — see
   *  Timeline.tsx's own reveal effect. Self-clears after ~600ms. */
  revealed: boolean;
  color: string;
  resizable: boolean;
  dragEnabled: boolean;
  onPointerDown: (clip: ClipItem, mode: DragMode, elRef?: React.RefObject<HTMLElement | null>) => (e: React.PointerEvent<HTMLElement>) => void;
  dragHandlers: {
    onPointerMove: (e: React.PointerEvent<HTMLElement>) => void;
    onPointerUp: (e: React.PointerEvent<HTMLElement>) => void;
    onPointerCancel: (e: React.PointerEvent<HTMLElement>) => void;
  };
}) {
  const top = clip.row * rowHeight + 2;
  // The clip's own bar element — resize handles are nested spans that
  // receive their own pointerdown, so useClipDrag needs an explicit ref to
  // this element to style THE CLIP (not the handle) during a resize. See
  // useClipDrag.ts's DragState.styleEl doc comment.
  const clipElRef = useRef<HTMLDivElement>(null);

  // 时间点（章节/facecam 关键帧）没有时长——画成菱形标记，画成条形就是在说谎。
  if (clip.isPoint) {
    return (
      <div
        ref={clipElRef}
        className={`marker${selected ? " marker--selected" : ""}${revealed ? " marker--revealed" : ""}`}
        style={{
          left: clip.from * pxPerFrame,
          top: top + (rowHeight - 11) / 2 - 2,
          background: color,
        }}
        title={`${clip.label} @ frame ${clip.from}`}
        onPointerDown={onPointerDown(clip, "move", clipElRef)}
        {...dragHandlers}
      />
    );
  }

  const width = Math.max(3, (clip.to - clip.from) * pxPerFrame);

  return (
    <div
      ref={clipElRef}
      className={
        "clip" +
        (selected ? " clip--selected" : "") +
        (invalid ? " clip--invalid" : "") +
        (clip.openEnded ? " clip--openended" : "") +
        (revealed ? " clip--revealed" : "")
      }
      style={{ left: clip.from * pxPerFrame, width, top, background: color }}
      title={
        clip.openEnded
          ? `${clip.label} — from frame ${clip.from}, stays on screen until the video ends (no end frame set)`
          : `${clip.label} — frames ${clip.from}–${clip.to}`
      }
      onPointerDown={onPointerDown(clip, "move", clipElRef)}
      {...dragHandlers}
    >
      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{clip.label}</span>
      {clip.openEnded && <span className="clip__inf" title="No end frame — runs to the end of the video">∞</span>}
      {resizable && dragEnabled && (
        <>
          <span
            className="clip__handle clip__handle--l"
            onPointerDown={onPointerDown(clip, "resize-l", clipElRef)}
            {...dragHandlers}
          />
          <span
            className="clip__handle clip__handle--r"
            onPointerDown={onPointerDown(clip, "resize-r", clipElRef)}
            {...dragHandlers}
          />
        </>
      )}
    </div>
  );
});
