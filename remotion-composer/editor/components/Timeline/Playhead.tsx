import React, { useEffect, useRef } from "react";
import { frameRef, subscribePlayhead } from "../../state/playhead";

type ScrubHandlers = {
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerMove: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerUp: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerCancel: (e: React.PointerEvent<HTMLElement>) => void;
};

/**
 * 播放头。位置**直接写 DOM**，不经过 React state——播放时 frameupdate 每帧
 * 触发一次，走 setState 会让整棵时间轴每秒重渲染 30 次。
 *
 * `.playhead` itself stays `pointer-events: none` (styles.css) — a
 * full-height grabbable strip would swallow pointerdowns meant for clip
 * resize handles wherever the playhead happens to sit. The grabbable
 * surface is a separate inner `.playhead__grip`, confined to the ruler
 * band (styles.css: `height: var(--ruler-h)`) and only rendered when
 * `scrubHandlers` is supplied (usePlayheadScrub.ts) — omit it for a purely
 * visual, non-interactive playhead.
 */
export function Playhead({
  pxPerFrame,
  labelWidth,
  height,
  scrubHandlers,
}: {
  pxPerFrame: number;
  labelWidth: number;
  height: number;
  scrubHandlers?: ScrubHandlers;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const apply = (frame: number) => {
      if (ref.current) ref.current.style.transform = `translateX(${frame * pxPerFrame}px)`;
    };
    apply(frameRef.current);          // 缩放变化后立刻回到正确位置
    return subscribePlayhead(apply);
  }, [pxPerFrame]);

  return (
    <div className="playhead" ref={ref} style={{ left: labelWidth, height }}>
      {scrubHandlers && <div className="playhead__grip" {...scrubHandlers} />}
    </div>
  );
}
