import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PlayerRef } from "@remotion/player";
import { compileAuthoredScene } from "./state/compileAuthoredScene";
import { framesToTimecode, setPlayheadFrame, frameRef, cutsRef } from "./state/playhead";
import { normalizeCuts, totalCutFrames, type VideoCut } from "../src/cuts";
import { AuthoredTimeline } from "./components/Authored/AuthoredTimeline";
import type { ManifestElement } from "./state/authoredHitTest";
import { useHistory } from "./state/history";
import { validateAuthored, authoredErrorIndex } from "./state/authoredValidation";
import { recoverMissingManifestElements, parseReservedRectDefaults } from "./state/recoverManifest";
import { CAPTION_ID_PREFIX } from "./components/Authored/AuthoredTimeline";
import {
  chunkWords, findChunkByKey,
  clampChunkFromSec, retimeChunkWords, applyChunkTextEdit, wordsBaseStamp,
  type Word,
} from "./state/authoredCaptions";
import { contentDefaults } from "./state/authoredKindSchemas";
import { AuthoredPreview } from "./components/Authored/AuthoredPreview";
import { AuthoredInspectorBody } from "./components/Authored/AuthoredInspectorBody";
import { AuthoredPhoneShell } from "./components/Phone/AuthoredPhoneShell";
import { useMediaQuery } from "./state/useMediaQuery";

// Matches App.tsx's own PHONE_BREAKPOINT_QUERY exactly — a mismatch would
// size touch-target rules for the wrong shell right at the boundary.
const PHONE_BREAKPOINT_QUERY = "(max-width: 860px)";

// Reserved id for the caption block's own position/size override — captions
// aren't a manifest element (no `kind`, no content fields; the burned-in
// text itself is governed by props.words + the timeline's caption chunks,
// see authoredCaptions.ts), so this is a synthetic pseudo-element that only
// exists to give the caption CONTAINER a draggable/resizable box on the
// canvas, same mechanism as any real manifest element's x/y/w/h override.
// A patched scene reads props.overrides?.["__captionBox"]?.x/y/w/h the same
// way it reads a real element's geometry.
const CAPTION_BOX_ID = "__captionBox";
// Matches the bottom/left/right values every demo scene's caption block was
// hand-patched to convert from (bottom:160/120, left/right:60/40) — used
// only when parseReservedRectDefaults can't find a real default in this
// job's own tsx (e.g. a scene that hasn't been patched/authored to wire
// __captionBox yet), so the drag box still starts somewhere reasonable
// instead of at (0,0).
const CAPTION_BOX_FALLBACK = { x: 60, y: 1560, w: 960, h: 200 };

type AuthoredData = {
  tsx: string;
  manifest: ManifestElement[];
  overrides: Record<string, Record<string, unknown>>;
  previewUrl: string | null;
  videoSrc: string;
  broll: { src: string; label: string; startFrame: number; endFrame: number }[];
  words: Word[];
  fps: number;
  durationInFrames: number;
  width: number;
  height: number;
  job_status: string;
  editor_state: string;
};

type SaveState = "idle" | "saving" | "rendering" | "done" | "failed";

async function apiGet(path: string, token: string) {
  const resp = await fetch(`${path}?token=${encodeURIComponent(token)}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
  return data;
}

async function apiPost(path: string, token: string, body: unknown): Promise<any> {
  const resp = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data?.detail || `HTTP ${resp.status}`) as Error & { status?: number };
    err.status = resp.status;
    throw err;
  }
  return data;
}

const NATIVE_W = 1080;
const NATIVE_H = 1920;

// 桌面 vs 触屏——按能力检测，不是按屏幕宽度。同 App.tsx 自己的
// detectDesktopPointer；也跟 AuthoredOverlay.tsx 的本地副本同一份逻辑,
// 两处都不共享同一个模块——这类一行小函数在这个代码库里就地复制，见
// AuthoredOverlay.tsx 自己的写法。
function detectDesktopPointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return true;
  return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
}

function blurActiveElement(): void {
  const el = document.activeElement as HTMLElement | null;
  // FrameInput / text inputs commit onBlur — undo must flush that first, or
  // the value gets undone while the input still shows the uncommitted text.
  el?.blur?.();
}

/**
 * Phase 8 — top-level shell for an Arm B (AI-authored) job. Deliberately a
 * SEPARATE component from `Editor` (App.tsx), not a mode flag threaded
 * through it: the two editing models share almost nothing under the hood
 * (no render_props.schema.json, no fixed section vocabulary, no cuts model)
 * — forcing them through one component would mean scattering `if (armB)`
 * branches through code that's already dense. `App`'s own top-level load
 * step decides which of the two ever mounts (see App.tsx).
 *
 * P8.6 shipped the live in-browser compiled preview (read-only). P8.7 added
 * click-to-select + drag reposition/resize (AuthoredOverlay, writing into a
 * local `overrides` object — instant, no server round-trip), a
 * manifest-kind-driven Inspector form (SchemaForm.tsx's ObjectField, fed
 * per-kind schemas above instead of render_props.schema.json), and Save
 * (POST /overrides, triggers a real re-render with no LLM call — mirrors
 * App.tsx's handleSave/status-poll pattern exactly).
 *
 * This component itself only owns the initial fetch — `AuthoredEditorInner`
 * requires real data to mount `useHistory` correctly (its snapshot-stack
 * seeds itself lazily on first render — see state/history.ts's own doc
 * comment), so it never mounts until the fetch resolves. Same App/Editor
 * split as App.tsx, same reason.
 */
export function AuthoredEditor({ jobId, token }: { jobId: string; token: string }) {
  const [data, setData] = useState<AuthoredData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiGet(`/api/editor/${encodeURIComponent(jobId)}/authored`, token)
      .then((d) => {
        if (!cancelled) setData(d as AuthoredData);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(String(e instanceof Error ? e.message : e));
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, token]);

  if (loadError) {
    return <div className="center-msg">Couldn't load this scene: {loadError}</div>;
  }
  if (!data) {
    return <div className="center-msg">Loading…</div>;
  }
  return <AuthoredEditorInner jobId={jobId} token={token} initialData={data} />;
}

function AuthoredEditorInner({
  jobId, token, initialData,
}: {
  jobId: string; token: string; initialData: AuthoredData;
}) {
  const data = initialData; // never changes post-mount — same convention as App.tsx's Editor/initialProps
  // Server manifest + client-recovered elements (see recoverManifest.ts) —
  // `data` never changes post-mount, so this only ever computes once.
  const manifest = useMemo(
    () => [...data.manifest, ...recoverMissingManifestElements(data.tsx, data.manifest)],
    [data.manifest, data.tsx]
  );

  // Synthetic caption-box pseudo-element (see CAPTION_BOX_ID's own comment)
  // — NOT part of `manifest` (it's not a real AI-authored element, has no
  // kind/content fields, and must never be validated/saved through the
  // regular manifest-element paths), only merged into what the CANVAS
  // overlay renders so it gets the same drag/resize box as any real
  // element. `layer: -1` (lower than any real content, which starts at 0)
  // so a real element occupying the same screen region always wins
  // click-priority; the caption box is still reachable via click-cycling.
  const captionBoxDefaultRect = useMemo(
    () => parseReservedRectDefaults(data.tsx, CAPTION_BOX_ID, CAPTION_BOX_FALLBACK),
    [data.tsx]
  );
  const captionBoxEl: ManifestElement = useMemo(() => ({
    id: CAPTION_BOX_ID,
    kind: "text_block",
    layer: -1,
    x: captionBoxDefaultRect.x,
    y: captionBoxDefaultRect.y,
    w: captionBoxDefaultRect.w,
    h: captionBoxDefaultRect.h ?? 200,
    mountFrame: 0,
  }), [captionBoxDefaultRect]);
  const overlayManifest = useMemo(() => [...data.manifest, captionBoxEl], [data.manifest, captionBoxEl]);

  const [compileError, setCompileError] = useState<string | null>(null);
  const [component, setComponent] = useState<React.ComponentType<Record<string, unknown>> | null>(null);
  const [playing, setPlaying] = useState(false);
  const playerRef = useRef<PlayerRef>(null);
  const timeElRef = useRef<HTMLSpanElement>(null);

  const { props: historyProps, commit, undo, redo, canUndo, canRedo } = useHistory(initialData.overrides);
  const overrides = historyProps as Record<string, Record<string, unknown>>;

  // Reserved `__cuts` overrides key (mid-video razor cuts) — same reserved-
  // key pattern as `__scene`/`__captionBox`, popped server-side in
  // _stage_workspace and written as top-level props.videoCuts. Stored as a
  // raw VideoCut[] (not a keyed object like `__scene`), so it can't go
  // through handleOverrideChange's merge-into-object semantics below —
  // handleCutsChange does a full replace/delete instead, same shape as
  // handleResetCaptions's delete-the-whole-key pattern.
  const cutsOverride = overrides.__cuts as unknown as VideoCut[] | undefined;
  const cuts = useMemo(
    () => normalizeCuts(cutsOverride, data.durationInFrames),
    [cutsOverride, data.durationInFrames]
  );
  // Keep the command-imperative cache (state/playhead.ts's cutsRef) in sync
  // for FrameInput's "jump to playhead" button — same reason/mechanism as
  // App.tsx's own identical effect for Arm A.
  useEffect(() => { cutsRef.current = cuts; }, [cuts]);
  // data.durationInFrames is SOURCE-space (server-pinned, never changes when
  // cuts are added — matches every AuthoredScene prop contract); this is the
  // OUTPUT length the Player/timeline actually use, mirroring App.tsx's own
  // durationInFrames/sourceDurationFrames split for Arm A.
  const outputDurationInFrames = useMemo(
    () => Math.max(1, data.durationInFrames - totalCutFrames(cuts)),
    [data.durationInFrames, cuts]
  );
  const handleCutsChange = useCallback((next: VideoCut[]) => {
    commit((prev) => {
      const p = prev as Record<string, Record<string, unknown>>;
      if (next.length === 0) {
        if (!("__cuts" in p)) return prev;
        const rest = { ...p };
        delete rest.__cuts;
        return rest;
      }
      return { ...p, __cuts: next as unknown as Record<string, unknown> };
    });
  }, [commit]);

  // First mount: present === initialData.overrides (same reference), so this
  // is a safe "already saved" baseline with no extra setState needed to catch up.
  const [savedOverrides, setSavedOverrides] = useState<Record<string, Record<string, unknown>>>(initialData.overrides);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const isDesktopPointer = useMemo(detectDesktopPointer, []);

  // isPhone (width, live-subscribed) is a DIFFERENT signal from
  // isDesktopPointer (pointer capability, read once) — same distinction
  // App.tsx's Editor draws for Arm A, same reason: isPhone drives LAYOUT
  // (which shell mounts below), isDesktopPointer continues to drive
  // INTERACTION (AuthoredTimeline's isTouch, still fed from the latter).
  // `?ui=phone`/`?ui=desktop` overrides it for testing on any device.
  const uiOverride = useMemo(() => new URLSearchParams(window.location.search).get("ui"), []);
  const widthIsPhone = useMediaQuery(PHONE_BREAKPOINT_QUERY);
  const isPhone = uiOverride === "phone" ? true : uiOverride === "desktop" ? false : widthIsPhone;

  // Video/Audio synthetic-lane imagery — same fetch App.tsx's Editor does
  // for Arm A (server-generated ffmpeg thumbnails/waveform, cached per
  // job). The /filmstrip and /waveform routes are arm-agnostic already;
  // what used to be missing on this arm was pipeline_runner._editor_disk_props
  // falling back to authored/props.json, not anything about this fetch.
  const [filmstripUrls, setFilmstripUrls] = useState<string[]>([]);
  const [waveformUrl, setWaveformUrl] = useState<string | null>(null);
  useEffect(() => {
    apiGet(`/api/editor/${encodeURIComponent(jobId)}/filmstrip`, token)
      .then((d) => setFilmstripUrls(Array.isArray(d.thumbnails) ? d.thumbnails : []))
      .catch(() => {});
    apiGet(`/api/editor/${encodeURIComponent(jobId)}/waveform`, token)
      .then((d) => setWaveformUrl(d.waveform || null))
      .catch(() => {});
  }, [jobId, token]);

  // Compile once, on mount — `data.tsx` never changes for the lifetime of
  // this component (see the `data` alias above), so no dependency array
  // subtlety is needed here the way the old single-component version had.
  useEffect(() => {
    const result = compileAuthoredScene(data.tsx);
    if (result.ok) {
      setComponent(() => result.component);
      setCompileError(null);
    } else {
      setComponent(null);
      setCompileError(result.error);
    }
  }, [data.tsx]);

  useEffect(() => {
    const player = playerRef.current;
    if (!player) return;
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    player.addEventListener("play", onPlay);
    player.addEventListener("pause", onPause);
    return () => {
      player.removeEventListener("play", onPlay);
      player.removeEventListener("pause", onPause);
    };
  }, [component]);

  // Timecode written directly to the DOM, not React state — `frameupdate`
  // fires every frame during playback (30fps); routing THAT through setState
  // would re-render this whole component every frame. Same pattern as
  // PreviewPane.tsx's own timeElRef. The frame itself is ALSO pushed into
  // the shared module-level store (state/playhead.ts) other components read
  // from (AuthoredOverlay for hit-test visibility, AuthoredTimeline for the
  // playhead marker) — that store's own listeners are responsible for
  // deciding whether/how to turn it into a render, this call site doesn't
  // need to know or care.
  useEffect(() => {
    const player = playerRef.current;
    if (!player) return;
    // player.getCurrentFrame() is OUTPUT-frame space (the Player's own
    // counter, over outputDurationInFrames post-Phase-3) — display both
    // halves of "X / Y" in that same space, not data.durationInFrames
    // (SOURCE, what AuthoredScene itself is authored against).
    const write = (frame: number) => {
      if (timeElRef.current) {
        timeElRef.current.textContent = `${framesToTimecode(frame)} / ${framesToTimecode(outputDurationInFrames)}`;
      }
      setPlayheadFrame(frame);
    };
    write(player.getCurrentFrame());
    const onFrame = (e: { detail: { frame: number } }) => write(e.detail.frame);
    player.addEventListener("frameupdate", onFrame);
    return () => player.removeEventListener("frameupdate", onFrame);
  }, [component, outputDurationInFrames]);

  useEffect(() => () => {
    if (pollRef.current) window.clearInterval(pollRef.current);
  }, []);

  // `coalesceKey` lets rapid-fire callers (Inspector typing, the volume
  // slider drag) merge into one undo step; drags/timeline commits pass the
  // default `null` — one step per gesture, same convention App.tsx uses.
  const handleOverrideChange = useCallback((id: string, patch: Record<string, unknown>, coalesceKey: string | null = null) => {
    commit((prev) => {
      const p = prev as Record<string, Record<string, unknown>>;
      return { ...p, [id]: { ...(p[id] || {}), ...patch } };
    }, { coalesceKey });
  }, [commit]);

  const handleSeek = useCallback((frame: number) => {
    playerRef.current?.seekTo(frame);
  }, []);

  // Pause/resume playback around a playhead scrub — same reasoning/pattern
  // as App.tsx's own handleScrubStart/handleScrubEnd for Arm A: seekTo
  // alone doesn't pause, so without this the video keeps advancing under a
  // continuous drag instead of parking on the frame being dragged to.
  const wasPlayingBeforeScrubRef = useRef(false);
  const handleScrubStart = useCallback(() => {
    wasPlayingBeforeScrubRef.current = playerRef.current?.isPlaying() ?? false;
    playerRef.current?.pause();
  }, []);
  const handleScrubEnd = useCallback(() => {
    if (wasPlayingBeforeScrubRef.current) playerRef.current?.play();
  }, []);

  const handleResetSelected = useCallback(() => {
    if (!selectedId) return;
    commit((prev) => {
      const p = prev as Record<string, Record<string, unknown>>;
      if (!(selectedId in p)) return prev;
      const next = { ...p };
      delete next[selectedId];
      return next;
    }, { coalesceKey: null });
  }, [selectedId, commit]);

  // Captions — reserved `__captions` overrides key, SEPARATE from `__scene`.
  // It must not ride on `__scene`: authored_renderer.py's _stage_workspace
  // spreads `__scene` FIRST specifically so a stray key inside it can never
  // clobber a computed prop — but `words` IS a computed prop, so putting a
  // words override inside `__scene` would make the edit silently vanish at
  // render time. `__captions` gets its own dedicated pop/apply on the
  // server (see authored_renderer.py's own comment on this).
  //
  // `effectiveWords` flows straight into inputProps.words (below) — the
  // compiled scene already reads `props.words` per the frozen authoring
  // contract, so a caption edit previews live with zero TSX change, exactly
  // like a manifest-element override does.
  const captionOverride = overrides.__captions as { words?: Word[] } | undefined;
  const effectiveWords = useMemo(
    () => (Array.isArray(captionOverride?.words) ? (captionOverride!.words as Word[]) : data.words),
    [captionOverride]
  );
  const captionChunks = useMemo(() => chunkWords(effectiveWords), [effectiveWords]);

  const commitCaptionWords = useCallback((newWords: Word[], coalesceKey: string | null) => {
    commit((prev) => {
      const p = prev as Record<string, Record<string, unknown>>;
      return { ...p, __captions: { words: newWords, base: wordsBaseStamp(data.words) } };
    }, { coalesceKey });
  }, [commit, data.words]);

  const handleCaptionRetime = useCallback((chunkKey: number, fromFrame: number, toFrame: number) => {
    const chunk = findChunkByKey(captionChunks, chunkKey);
    if (!chunk) return;
    const fps = data.fps;
    let fromSec = fromFrame / fps;
    const toSec = toFrame / fps;
    fromSec = clampChunkFromSec(captionChunks, chunkKey, fromSec);
    const newWords = retimeChunkWords(effectiveWords, chunk, fromSec, toSec, fps);
    commitCaptionWords(newWords, null);
  }, [captionChunks, effectiveWords, data.fps, commitCaptionWords]);

  const handleCaptionTextEdit = useCallback((chunkKey: number, newText: string) => {
    const chunk = findChunkByKey(captionChunks, chunkKey);
    if (!chunk) return;
    const newWords = applyChunkTextEdit(effectiveWords, chunk, newText);
    if (!newWords) return; // rejected (empty text) — caller's input keeps its prior value
    commitCaptionWords(newWords, `captions:text:${chunkKey}`);
    // No need to update selectedId/re-derive a key here: chunkWords()'s
    // boundary decision at `chunk.startIndex` depends only on words at/
    // before that index, which an in-place splice never touches — so
    // `chunkKey` (== chunk.startIndex) is provably still a chunk's `key`
    // after re-chunking, confirmed by direct testing against a real job's
    // full word list (every one of 43 chunks, expanded by a word-count-
    // changing edit, remained findable by the SAME key with zero fallback
    // needed). findChunkContainingIndex exists as a defensive fallback for
    // findChunkByKey callers that can't rely on this invariant holding
    // (e.g. a future caller editing multiple chunks in one batch), not
    // because this call site needs it.
  }, [captionChunks, effectiveWords, commitCaptionWords]);

  const handleResetCaptions = useCallback(() => {
    commit((prev) => {
      const p = prev as Record<string, Record<string, unknown>>;
      if (!("__captions" in p)) return prev;
      const next = { ...p };
      delete next.__captions;
      return next;
    }, { coalesceKey: null });
  }, [commit]);

  const isDirty = overrides !== savedOverrides;

  // Effective (contentDefaults + overrides) value per element — the same
  // merge the Inspector already feeds ObjectField's `value`, generalized to
  // every element so a bad value elsewhere still blocks Save even while
  // looking at something else.
  const effectiveValues = useMemo(() => {
    const out: Record<string, Record<string, unknown>> = {};
    for (const el of manifest) {
      out[el.id] = { ...contentDefaults(el), ...(overrides[el.id] || {}) };
    }
    return out;
  }, [manifest, overrides]);
  const validation = useMemo(
    () => validateAuthored(manifest, effectiveValues, data.durationInFrames),
    [manifest, effectiveValues, data.durationInFrames]
  );
  const errIdx = useMemo(() => authoredErrorIndex(validation.errors), [validation.errors]);

  const handleSave = useCallback(async () => {
    setSaveState("saving");
    setSaveError(null);
    const sent = overrides; // capture what's actually sent, not whatever overrides is once polling resolves
    try {
      await apiPost(`/api/editor/${encodeURIComponent(jobId)}/overrides`, token, sent);
      setSaveState("rendering");
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        try {
          const status = await apiGet(`/api/editor/${encodeURIComponent(jobId)}/status`, token);
          if (status.state !== "rendering") {
            if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
            if (status.error) {
              setSaveState("failed");
              setSaveError(status.error);
            } else {
              setSaveState("done");
              setSavedOverrides(sent);
            }
          }
        } catch { /* one failed poll isn't conclusive — server may be briefly unreachable */ }
      }, 3000);
    } catch (e) {
      const status = (e as { status?: number })?.status;
      setSaveState("failed");
      setSaveError(
        status === 429
          ? "Hourly save limit reached — try again in a bit."
          : String(e instanceof Error ? e.message : e)
      );
    }
  }, [jobId, token, overrides]);

  // ── Keyboard shortcuts ──────────────────────────────────────────────
  // Ported from App.tsx's own onKeyDown block. Skips Delete/Backspace —
  // Arm B can't delete model-authored elements, only reset their overrides.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const isMeta = e.ctrlKey || e.metaKey;
      const tag = (document.activeElement as HTMLElement | null)?.tagName;
      const inTextInput = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";

      if (isMeta && e.key.toLowerCase() === "z") {
        e.preventDefault();
        blurActiveElement();
        if (e.shiftKey) redo(); else undo();
        return;
      }
      if (isMeta && e.key.toLowerCase() === "y") {
        e.preventDefault();
        blurActiveElement();
        redo();
        return;
      }
      if (inTextInput) return; // don't steal Space/arrows/Esc while typing

      if (e.key === " ") { e.preventDefault(); playerRef.current?.toggle(); return; }
      if (e.key === "Escape") { setSelectedId(null); return; }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const dir = e.key === "ArrowLeft" ? -1 : 1;
        const step = e.shiftKey ? 10 : 1;
        playerRef.current?.seekTo(Math.max(0, frameRef.current + dir * step));
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [undo, redo]);

  const sceneOverrides = (overrides.__scene || {}) as { videoVolume?: number };
  const videoVolume = typeof sceneOverrides.videoVolume === "number" ? sceneOverrides.videoVolume : 1;

  const inputProps = {
    videoSrc: data.videoSrc,
    broll: data.broll,
    words: effectiveWords,
    fps: data.fps,
    durationInFrames: data.durationInFrames,
    width: data.width,
    height: data.height,
    // Scene-level (not per-element) — the compiled scene reads
    // props.videoVolume directly on its base OffthreadVideo (same prop
    // path authored_renderer.py merges onto the real render's props), so
    // this is genuinely live in this in-browser preview too, unlike Arm
    // A's musicVolume which is only mixed in server-side after render.
    videoVolume,
    videoCuts: cuts,
    overrides,
  };

  const selectedEl = selectedId && !selectedId.startsWith(CAPTION_ID_PREFIX)
    ? manifest.find((m) => m.id === selectedId) ?? null
    : null;
  const selectedCaptionKey = selectedId?.startsWith(CAPTION_ID_PREFIX)
    ? Number(selectedId.slice(CAPTION_ID_PREFIX.length))
    : null;
  // findChunkByKey alone is sufficient here — see handleCaptionTextEdit's
  // own comment on why `chunkKey` (== chunk.startIndex) provably survives
  // re-chunking even after a word-count-changing edit.
  const selectedCaptionChunk = selectedCaptionKey == null
    ? null
    : findChunkByKey(captionChunks, selectedCaptionKey) ?? null;
  const selectedCaptionBox = selectedId === CAPTION_BOX_ID;
  const captionBoxOverride = overrides[CAPTION_BOX_ID] as { x?: number; y?: number; w?: number; h?: number } | undefined;
  const effectiveCaptionBoxRect = {
    x: captionBoxOverride?.x ?? captionBoxEl.x,
    y: captionBoxOverride?.y ?? captionBoxEl.y,
    w: captionBoxOverride?.w ?? captionBoxEl.w,
    h: captionBoxOverride?.h ?? captionBoxEl.h,
  };
  const busy = saveState === "saving" || saveState === "rendering";

  // Position/size only became a LIVE override (vs. just descriptive manifest
  // metadata) once scene_author.py's prompt started requiring it — a scene
  // authored before that fix hardcodes `left: "90px"` etc. matching the
  // manifest's numbers, so dragging silently writes overrides.x/y that the
  // component never reads. Scene-level check (not per-element): a stricter
  // per-id regex risks false negatives on legitimately-decorative elements
  // that never needed x/y wiring in the first place.
  const scenePredatesPositionEditing =
    data.manifest.length > 0 && !/overrides\?\.\[[^\]]+\]\?\.x\b/.test(data.tsx);

  if (isPhone) {
    return (
      <AuthoredPhoneShell
        jobId={jobId}
        jobStatus={data.job_status}
        isDirty={isDirty}
        canUndo={canUndo}
        canRedo={canRedo}
        onUndo={() => { blurActiveElement(); undo(); }}
        onRedo={() => { blurActiveElement(); redo(); }}
        onSave={handleSave}
        saveState={saveState}
        saveError={saveError}
        onDismissSaveError={() => setSaveState("idle")}
        busy={busy}
        validation={validation}
        scenePredatesPositionEditing={scenePredatesPositionEditing}
        component={component}
        inputProps={inputProps}
        durationInFrames={data.durationInFrames}
        outputDurationInFrames={outputDurationInFrames}
        fps={data.fps}
        width={data.width}
        height={data.height}
        playerRef={playerRef}
        manifest={overlayManifest}
        overrides={overrides}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onOverlayCommit={handleOverrideChange}
        previewUrl={data.previewUrl}
        compileError={compileError}
        filmstripUrls={filmstripUrls}
        waveformUrl={waveformUrl}
        effectiveWords={effectiveWords}
        onCaptionRetime={handleCaptionRetime}
        onSeek={handleSeek}
        onScrubStart={handleScrubStart}
        onScrubEnd={handleScrubEnd}
        hasItemError={(id) => errIdx.forItem(id)}
        onElementTimeCommit={handleOverrideChange}
        selectedEl={selectedEl}
        selectedCaptionChunk={selectedCaptionChunk}
        selectedCaptionBox={selectedCaptionBox}
        captionBoxRect={effectiveCaptionBoxRect}
        effectiveValues={effectiveValues}
        errorForField={(id, field) => errIdx.forField(id, field)}
        onElementChange={handleOverrideChange}
        onResetSelected={handleResetSelected}
        onCaptionTextEdit={handleCaptionTextEdit}
        onResetCaptions={handleResetCaptions}
        videoVolume={videoVolume}
        onVideoVolumeChange={(value) => handleOverrideChange("__scene", { videoVolume: value }, "scene:videoVolume")}
        cuts={cuts}
        onCutsChange={handleCutsChange}
      />
    );
  }

  return (
    <div className="app authored-app">
      <div className="toolbar">
        <span className="toolbar__title">OpenMontage — AI-authored</span>
        <span className="toolbar__meta">{jobId}</span>
        <button type="button" className="btn btn--icon btn--ghost" onClick={() => { blurActiveElement(); undo(); }}
          disabled={!canUndo} title="Undo (Ctrl+Z)">↶</button>
        <button type="button" className="btn btn--icon btn--ghost" onClick={() => { blurActiveElement(); redo(); }}
          disabled={!canRedo} title="Redo (Ctrl+Shift+Z)">↷</button>
        <span className="toolbar__spacer" />
        <span className="toolbar__meta" title={`Job status: ${data.job_status}`}>
          <span className={`statusdot statusdot--${busy ? "busy" : isDirty ? "dirty" : "clean"}`} />
          {" "}
          {busy ? (saveState === "saving" ? "Saving…" : "Rendering…") : isDirty ? "Unsaved changes" : "Saved"}
        </span>
        <button
          type="button"
          className="btn btn--primary"
          onClick={handleSave}
          disabled={!isDirty || busy || !validation.valid}
          title={
            !validation.valid
              ? "Fix the highlighted fields before saving"
              : !isDirty
              ? "No changes to save"
              : "Render your edits and send the new preview to WhatsApp"
          }
        >
          {busy ? "Working…" : "Save & send to WhatsApp"}
        </button>
      </div>

      {!validation.valid && !busy && (
        <div className="banner banner--warn" style={{ position: "fixed", bottom: 12, left: "50%", transform: "translateX(-50%)", zIndex: 50, maxWidth: 520 }}>
          {validation.errors.length} validation {validation.errors.length === 1 ? "issue" : "issues"} — fix the highlighted fields before saving.
        </div>
      )}

      {scenePredatesPositionEditing && (
        // Plain-flow placement would auto-place into this CSS grid's
        // implicit slot (every declared area is already claimed by
        // something else) — position:fixed sidesteps that, same reasoning
        // as the save-error .toast below, just anchored to the top so the
        // two never overlap.
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
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => setSaveState("idle")}>✕</button>
        </div>
      )}

      <div className="authored-app__center">
        <div className="preview">
          <div className="preview__viewport authored-app__viewport">
            <AuthoredPreview
              className="authored-app__player"
              component={component}
              inputProps={inputProps}
              durationInFrames={data.durationInFrames}
              fps={data.fps}
              width={data.width}
              height={data.height}
              playerRef={playerRef}
              // Deliberately overlayManifest (server-authored elements +
              // the synthetic caption box), NOT the merged `manifest` used
              // elsewhere — recovered elements' x/y/w/h are placeholders
              // (0,0,0,0), not real geometry, so they must never appear as
              // a draggable/clickable box on the canvas. They're still
              // reachable via the Timeline (mountFrame/endFrame are
              // genuinely wired) and the Inspector, both of which use
              // `manifest`. The caption box IS meant to be draggable here,
              // unlike a recovered element — its geometry comes from
              // parseReservedRectDefaults reading the scene's own real
              // wired default, not a placeholder.
              manifest={overlayManifest}
              overrides={overrides}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onCommit={handleOverrideChange}
              previewUrl={data.previewUrl}
              compileError={compileError}
            />
          </div>
          <div className="preview__transport">
            <button
              type="button"
              className="btn btn--icon"
              onClick={() => playerRef.current?.toggle()}
              disabled={!component}
              title={playing ? "Pause (Space)" : "Play (Space)"}
            >
              {playing ? "❚❚" : "▶"}
            </button>
            <button
              type="button"
              className="btn btn--icon"
              onClick={() => playerRef.current?.seekTo(0)}
              disabled={!component}
              title="Back to start"
            >
              ⏮
            </button>
            <span className="preview__time" ref={timeElRef}>{framesToTimecode(0)} / {framesToTimecode(outputDurationInFrames)}</span>
          </div>
        </div>
      </div>

      <div className="inspector authored-app__inspector">
        <AuthoredInspectorBody
          selectedEl={selectedEl}
          selectedCaptionChunk={selectedCaptionChunk}
          selectedCaptionBox={selectedCaptionBox}
          captionBoxRect={effectiveCaptionBoxRect}
          effectiveValues={effectiveValues}
          errorForField={(id, field) => errIdx.forField(id, field)}
          onElementChange={handleOverrideChange}
          onResetSelected={handleResetSelected}
          overrides={overrides}
          onCaptionTextEdit={handleCaptionTextEdit}
          onResetCaptions={handleResetCaptions}
          fps={data.fps}
          videoVolume={videoVolume}
          onVideoVolumeChange={(value) => handleOverrideChange("__scene", { videoVolume: value }, "scene:videoVolume")}
          onDeselect={() => setSelectedId(null)}
          cuts={cuts}
          sourceDurationFrames={data.durationInFrames}
          onCutsChange={handleCutsChange}
        />
      </div>

      <AuthoredTimeline
        className="authored-app__timeline"
        manifest={manifest}
        overrides={overrides}
        durationInFrames={outputDurationInFrames}
        cuts={cuts}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onCommit={handleOverrideChange}
        onCaptionRetime={handleCaptionRetime}
        onSeek={handleSeek}
        hasItemError={(id) => errIdx.forItem(id)}
        isTouch={!isDesktopPointer}
        filmstripUrls={filmstripUrls}
        waveformUrl={waveformUrl}
        broll={data.broll}
        words={effectiveWords}
        fps={data.fps}
        onScrubStart={handleScrubStart}
        onScrubEnd={handleScrubEnd}
      />
    </div>
  );
}
