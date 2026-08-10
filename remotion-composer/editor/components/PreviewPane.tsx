import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Player, PlayerRef } from "@remotion/player";
import { XiaojinEditorial, XiaojinEditorialProps } from "../../src/XiaojinEditorial";
import { computeOutputDuration } from "../../src/cuts";
import { FPS, frameRef, framesToTimecode, setPlayheadFrame, subscribePlayhead } from "../state/playhead";
import { PreviewOverlay } from "./Preview/PreviewOverlay";

const NATIVE_W = 1080;
const NATIVE_H = 1920;
const ZOOM_MIN = 0.1;
const ZOOM_MAX = 3;
const ZOOM_BUTTON_STEP = 1.25;
const ZOOM_WHEEL_STEP = 1.1;

type ZoomMode = "fit" | number;

function clampZoom(z: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
}

/** Largest zoom that fits the whole 1080x1920 composition inside a box of this size. */
function computeFitZoom(viewportW: number, viewportH: number): number | null {
  if (viewportW <= 0 || viewportH <= 0) return null;
  return Math.min(viewportW / NATIVE_W, viewportH / NATIVE_H);
}

/**
 * 预览区。持有 PlayerRef，把 `frameupdate` 桥接到播放头 store。
 *
 * `<Player>` 不会执行 calculateXiaojinEditorialMetadata（那个钩子只在真正
 * 渲染/CLI 时触发，Stage-0 spike 已确认），所以 durationInFrames 必须在这里
 * 按合成自己的算法手算一遍。
 *
 * React.memo 是必须的：props 每次变化都会重渲染整个 XiaojinEditorial 合成
 * （26 个数组 map 出约 40 个卡片组件，且没有任何 React.memo），不能让父组件
 * 的无关状态变化（选中项、缩放、保存状态…）也把它拖下水。
 */
export const PreviewPane = React.memo(function PreviewPane({
  props,
  playerRef,
  onPlayingChange,
  onItemChange,
  onSelect,
}: {
  props: XiaojinEditorialProps;
  playerRef: React.RefObject<PlayerRef | null>;
  onPlayingChange?: (playing: boolean) => void;
  /**
   * Stable (useCallback'd in App.tsx) — safe to pass as a normal prop
   * without breaking this component's memo the way a raw `selection` value
   * would (see PreviewOverlay.tsx / selectionBridge.ts for how selection
   * itself reaches the overlay instead).
   */
  onItemChange: (section: string, index: number | null, next: Record<string, unknown>) => void;
  /** Same stability requirement as onItemChange — click-to-select (Phase 6, F1). */
  onSelect: (target: { section: string; index: number | null } | null) => void;
}) {
  // Single source of truth (src/cuts.ts) — <Player> can't run
  // calculateXiaojinEditorialMetadata itself (see this component's own doc
  // comment above), so this must call the exact same function the
  // composition and App.tsx both use. Hand-duplicating this formula already
  // caused a real, confirmed bug once this session (the Player's own
  // duration readout stayed stale after a trim because this file had its
  // own copy that didn't get updated) — do not reintroduce a fourth copy.
  const durationInFrames = computeOutputDuration(props);
  const timeElRef = useRef<HTMLSpanElement>(null);
  const [playing, setPlaying] = useState(false);
  const prevDurationRef = useRef(durationInFrames);

  // Zoom/pan (Phase A). Size-based, not CSS transform:scale() — an overlay
  // added on top of the Player later needs getBoundingClientRect() on this
  // box to exactly equal the composition's rendered pixel box, which a CSS
  // transform would throw off (the box's own rect stays at 1x while its
  // painted content scales).
  const viewportRef = useRef<HTMLDivElement>(null);
  const [zoomMode, setZoomMode] = useState<ZoomMode>("fit");
  const [viewportSize, setViewportSize] = useState<{ w: number; h: number } | null>(null);

  // Measure the viewport synchronously before the first paint so there's
  // never a user-visible frame at the pre-measurement CSS fallback size —
  // ResizeObserver's own callback only fires async, so it can't be the only
  // source of the first measurement.
  useLayoutEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    setViewportSize({ w: el.clientWidth, h: el.clientHeight });
    const ro = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      setViewportSize({ w: entry.contentRect.width, h: entry.contentRect.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const fitZoom = viewportSize ? computeFitZoom(viewportSize.w, viewportSize.h) : null;
  const effectiveZoom = zoomMode === "fit" ? fitZoom : zoomMode;

  // fitZoom only changes via the ResizeObserver above, but a burst of
  // same-tick events (see stepZoom's comment) could still read it from a
  // stale render closure — mirror it into a ref so every step always sees
  // the current value regardless of how many renders have actually flushed.
  const fitZoomRef = useRef(fitZoom);
  fitZoomRef.current = fitZoom;

  // Multiplies the CURRENT zoom by `factor`, expressed as a setState
  // updater rather than reading `zoomMode` from the render closure.
  // Confirmed live: a burst of same-tick events (rapid clicks, or a fast
  // trackpad ctrl+scroll firing several `wheel` events before React
  // re-renders) all closed over the SAME stale `zoomMode`, so only the
  // first step in the burst actually applied — 8 rapid "zoom out" clicks
  // measurably collapsed to one step. The updater form composes off
  // React's own latest-state queue instead, so every step in a burst
  // applies in sequence no matter how many renders have flushed.
  const stepZoom = useCallback((factor: number) => {
    setZoomMode((prev) => {
      const base = prev === "fit" ? fitZoomRef.current ?? 1 : prev;
      return clampZoom(base * factor);
    });
  }, []);

  const zoomOut = useCallback(() => stepZoom(1 / ZOOM_BUTTON_STEP), [stepZoom]);
  const zoomIn = useCallback(() => stepZoom(ZOOM_BUTTON_STEP), [stepZoom]);
  const zoomToFit = useCallback(() => setZoomMode("fit"), []);
  const zoomTo100 = useCallback(() => setZoomMode(1), []);

  // Ctrl/Cmd+wheel to zoom. Must be a native, non-passive listener: React
  // attaches onWheel as a passive listener by default (matches the browser
  // default for scroll perf), so e.preventDefault() inside a JSX onWheel
  // handler is silently ignored and the page/container scrolls anyway.
  // Registered once (empty deps) — stepZoom is itself stable and reads
  // fresh state via the updater form, so this listener never needs to be
  // torn down and re-attached as zoom changes.
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const onWheelNative = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const factor = e.deltaY < 0 ? ZOOM_WHEEL_STEP : 1 / ZOOM_WHEEL_STEP;
      stepZoom(factor);
    };
    el.addEventListener("wheel", onWheelNative, { passive: false });
    return () => el.removeEventListener("wheel", onWheelNative);
  }, [stepZoom]);

  // Middle-button drag to pan. Deliberately NOT space-hold-drag: Space is
  // already a global play/pause shortcut (App.tsx's window keydown
  // handler), so overloading it here would fight that binding every time a
  // pan gesture started or ended near a frame boundary.
  const panRef = useRef<{ startX: number; startY: number; scrollLeft: number; scrollTop: number } | null>(null);

  const onViewportPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 1) return;
    e.preventDefault(); // suppress the browser's native middle-click autoscroll UI
    const el = viewportRef.current;
    if (!el) return;
    el.setPointerCapture(e.pointerId);
    panRef.current = { startX: e.clientX, startY: e.clientY, scrollLeft: el.scrollLeft, scrollTop: el.scrollTop };
  }, []);

  const onViewportPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const p = panRef.current;
    const el = viewportRef.current;
    if (!p || !el) return;
    el.scrollLeft = p.scrollLeft - (e.clientX - p.startX);
    el.scrollTop = p.scrollTop - (e.clientY - p.startY);
  }, []);

  const endPan = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!panRef.current) return;
    panRef.current = null;
    try { viewportRef.current?.releasePointerCapture(e.pointerId); } catch { /* already released */ }
  }, []);

  const playerBoxStyle: React.CSSProperties = effectiveZoom
    ? { width: NATIVE_W * effectiveZoom, height: NATIVE_H * effectiveZoom, flexShrink: 0 }
    : {};

  // frameupdate -> 播放头 store。这是全应用唯一的帧号来源。
  useEffect(() => {
    const player = playerRef.current;
    if (!player) return;

    const onFrame = (e: { detail: { frame: number } }) => setPlayheadFrame(e.detail.frame);
    const onPlay = () => { setPlaying(true); onPlayingChange?.(true); };
    const onPause = () => { setPlaying(false); onPlayingChange?.(false); };

    player.addEventListener("frameupdate", onFrame);
    player.addEventListener("play", onPlay);
    player.addEventListener("pause", onPause);
    return () => {
      player.removeEventListener("frameupdate", onFrame);
      player.removeEventListener("play", onPlay);
      player.removeEventListener("pause", onPause);
    };
    // playerRef 是稳定的 ref 对象；onPlayingChange 由调用方用 useCallback 固定。
  }, [playerRef, onPlayingChange]);

  // A cut commit can shrink durationInFrames below the current playhead
  // (e.g. cutting out the back half of the video while paused near the old
  // end) — <Player> clamps its OWN internal position silently, but the
  // custom transport (timecode text, our own playhead store) doesn't know
  // that happened unless told. Explicitly re-seek to the new valid range
  // whenever duration shrinks under the current frame.
  useEffect(() => {
    if (durationInFrames < prevDurationRef.current && frameRef.current >= durationInFrames) {
      playerRef.current?.seekTo(Math.max(0, durationInFrames - 1));
    }
    prevDurationRef.current = durationInFrames;
  }, [durationInFrames, playerRef]);

  // 时间码用直接写 DOM 更新——每帧一次 setState 会把整棵树拖垮。
  // 订阅回调只在下一次真实的帧变化（播放/seek）时才会跑——如果 durationInFrames
  // 因为一次 trim 提交而变了，但播放头这一刻没有移动（暂停中，没人 seek），
  // 文本会一直停在旧值，直到用户碰一下播放/seek。这里在（重新）订阅的同一时刻
  // 用当前 frameRef 立即写一次，把这个"要等下一次事件才刷新"的窗口关掉。
  useEffect(() => {
    if (timeElRef.current) {
      timeElRef.current.textContent = `${framesToTimecode(frameRef.current)} / ${framesToTimecode(durationInFrames)}`;
    }
    return subscribePlayhead((frame) => {
      if (timeElRef.current) {
        timeElRef.current.textContent = `${framesToTimecode(frame)} / ${framesToTimecode(durationInFrames)}`;
      }
    });
  }, [durationInFrames]);

  return (
    <div className="preview">
      <div
        className="preview__viewport"
        ref={viewportRef}
        onPointerDown={onViewportPointerDown}
        onPointerMove={onViewportPointerMove}
        onPointerUp={endPan}
        onPointerCancel={endPan}
      >
        <div className="preview__player" style={playerBoxStyle}>
          <Player
            ref={playerRef as React.RefObject<PlayerRef>}
            component={XiaojinEditorial}
            inputProps={props}
            durationInFrames={durationInFrames}
            fps={FPS}
            compositionWidth={1080}
            compositionHeight={1920}
            style={{ width: "100%", height: "100%" }}
            acknowledgeRemotionLicense
            // 自定义时间轴/快捷键接管这些交互，避免和 Player 内置的抢：
            controls={false}
            clickToPlay={false}
            doubleClickToFullscreen={false}
            spaceKeyToPlayOrPause={false}
          />
          <PreviewOverlay
            scale={effectiveZoom}
            props={props as unknown as Record<string, unknown>}
            durationInFrames={durationInFrames}
            onItemChange={onItemChange}
            onSelect={onSelect}
          />
        </div>
      </div>
      <div className="preview__transport">
        <button
          type="button"
          className="btn btn--icon"
          onClick={() => playerRef.current?.toggle()}
          title={playing ? "Pause (Space)" : "Play (Space)"}
        >
          {playing ? "❚❚" : "▶"}
        </button>
        <button
          type="button"
          className="btn btn--icon"
          onClick={() => playerRef.current?.seekTo(0)}
          title="Back to start"
        >
          ⏮
        </button>
        <span className="preview__time" ref={timeElRef}>
          {framesToTimecode(0)} / {framesToTimecode(durationInFrames)}
        </span>
        <div className="preview__zoom">
          <button type="button" className="btn btn--sm btn--ghost" onClick={zoomOut} title="Zoom out (Ctrl+scroll)">−</button>
          <button type="button" className="btn btn--sm btn--ghost" onClick={zoomToFit} title="Fit to window">Fit</button>
          <span className="preview__zoom-label">{effectiveZoom ? `${Math.round(effectiveZoom * 100)}%` : "…"}</span>
          <button type="button" className="btn btn--sm btn--ghost" onClick={zoomTo100} title="Zoom to 100%">100%</button>
          <button type="button" className="btn btn--sm btn--ghost" onClick={zoomIn} title="Zoom in (Ctrl+scroll)">+</button>
        </div>
      </div>
    </div>
  );
});
