import React, { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Player, type PlayerRef } from "@remotion/player";
import { AuthoredOverlay } from "./AuthoredOverlay";
import type { ManifestElement } from "../../state/authoredHitTest";
import { AuthoredCutWrapper } from "../../../src/components/xiaojin/AuthoredCutWrapper";
import { normalizeCuts, totalCutFrames, type VideoCut } from "../../../src/cuts";

const NATIVE_W = 1080;

/**
 * The live compiled-scene preview (Player + click/drag overlay) with its
 * already-rendered-video fallback — extracted from AuthoredEditor.tsx
 * (Phase 5, phone shell) so the desktop layout and AuthoredPhoneShell.tsx
 * render byte-for-byte the same preview, just inside different chrome
 * (`.preview__player` sidebar vs a phone-sized box).
 *
 * Owns its OWN box ref + scale measurement (CSS px per composition px,
 * for AuthoredOverlay's hit-test/drag math) internally, rather than
 * receiving `scale` as a prop — the player box's rendered width differs
 * between the desktop and phone layouts (different CSS containers), so
 * each mounted instance measuring its own box is what makes both call
 * sites correct automatically, instead of one of them silently reusing a
 * scale computed from the OTHER layout's box size.
 *
 * Also the ONE Player mount point for Arm B, which is why cuts-awareness
 * lives here rather than in either caller: `component` (the compiled,
 * transparent-background AuthoredScene) is wrapped in the same
 * `AuthoredCutWrapper` the real server render's Root.tsx mounts (Phase 1/2
 * of the cuts feature), so `props.sourceFrame` and the cut-aware base
 * video are byte-identical between live preview and real render. `inputProps`
 * carries `durationInFrames` in SOURCE-frame space (matches what
 * AuthoredScene itself is authored against); the value actually handed to
 * `<Player>` is the OUTPUT (cut-shortened) duration, computed here so
 * neither AuthoredEditor.tsx nor AuthoredPhoneShell.tsx has to duplicate
 * the same `normalizeCuts`/`totalCutFrames` math.
 */
export function AuthoredPreview({
  component,
  inputProps,
  durationInFrames,
  fps,
  width,
  height,
  playerRef,
  manifest,
  overrides,
  selectedId,
  onSelect,
  onCommit,
  previewUrl,
  compileError,
  className,
}: {
  component: React.ComponentType<Record<string, unknown>> | null;
  inputProps: Record<string, unknown>;
  durationInFrames: number;
  fps: number;
  width: number;
  height: number;
  playerRef: React.RefObject<PlayerRef>;
  /** Deliberately the server-authored manifest only (never the client-
   *  recovered superset) — see the call site's own comment on why: a
   *  recovered element's x/y/w/h are placeholders, not real geometry, and
   *  must never appear as a draggable/clickable box on the canvas. */
  manifest: ManifestElement[];
  overrides: Record<string, Record<string, unknown>>;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onCommit: (id: string, patch: { x?: number; y?: number; w?: number; h?: number }) => void;
  previewUrl: string | null;
  compileError: string | null;
  className?: string;
}) {
  const playerBoxRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState<number | null>(null);

  // `durationInFrames` (the prop) is SOURCE-space; `inputProps.videoCuts`
  // is undefined until Phase 4's cuts UI writes `overrides.__cuts` —
  // normalizeCuts/totalCutFrames both already treat undefined as "no
  // cuts," so this is a no-op (outputDurationInFrames === durationInFrames)
  // until a real cut exists.
  const videoCuts = (inputProps as { videoCuts?: VideoCut[] }).videoCuts;
  const normalizedCuts = useMemo(
    () => normalizeCuts(videoCuts, durationInFrames),
    [videoCuts, durationInFrames]
  );
  const outputDurationInFrames = Math.max(1, durationInFrames - totalCutFrames(normalizedCuts));

  // Mirrors authored_renderer.py's generated Root.tsx `AuthoredCutRoot`
  // exactly (same wrapper, same prop wiring) — the one implementation the
  // live preview and the real render both defer to, never duplicated.
  const cutAwareComponent = useMemo(() => {
    if (!component) return null;
    const Wrapped: React.FC<Record<string, unknown>> = (props) => (
      <AuthoredCutWrapper
        videoSrc={props.videoSrc as string}
        videoVolume={props.videoVolume as number | undefined}
        cuts={props.videoCuts as VideoCut[] | undefined}
        sourceDurationFrames={durationInFrames}
        component={component}
        sceneProps={props}
      />
    );
    return Wrapped;
  }, [component, durationInFrames]);

  // The player box self-sizes via styles.css's `aspect-ratio: 1080/1920` +
  // `height:100%`, so this just reads the resulting rendered width back
  // out, same measure-after-layout approach PreviewPane.tsx uses for its
  // own zoom-driven box (just simpler here: no zoom controls, the box's
  // CSS-derived size IS the scale basis).
  useLayoutEffect(() => {
    const el = playerBoxRef.current;
    if (!el) return;
    const measure = () => setScale(el.clientWidth > 0 ? el.clientWidth / NATIVE_W : null);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div className={`preview__player${className ? ` ${className}` : ""}`} ref={playerBoxRef}>
      {cutAwareComponent ? (
        <>
          <Player
            ref={playerRef}
            component={cutAwareComponent}
            inputProps={inputProps}
            durationInFrames={outputDurationInFrames}
            fps={fps}
            compositionWidth={width}
            compositionHeight={height}
            style={{ width: "100%", height: "100%" }}
            acknowledgeRemotionLicense
            controls={false}
            clickToPlay={false}
            doubleClickToFullscreen={false}
            spaceKeyToPlayOrPause={false}
          />
          <AuthoredOverlay
            scale={scale}
            manifest={manifest}
            overrides={overrides}
            selectedId={selectedId}
            onSelect={onSelect}
            onCommit={onCommit}
          />
        </>
      ) : (
        <div className="authored-app__fallback">
          {previewUrl ? (
            <video src={previewUrl} controls style={{ maxWidth: "100%", maxHeight: "100%" }} />
          ) : (
            <div className="inspector__empty">No preview available yet.</div>
          )}
          {compileError && (
            <div className="authored-app__compile-note">
              Live preview unavailable — showing the last rendered video instead.
              <details>
                <summary>Details</summary>
                {compileError}
              </details>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
