import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PlayerRef } from "@remotion/player";
import renderPropsSchema from "../../contracts/render_props.schema.json";
import type { XiaojinEditorialProps } from "../src/XiaojinEditorial";
import { PreviewPane } from "./components/PreviewPane";
import { Toolbar } from "./components/Toolbar";
import { Inspector } from "./components/Inspector/Inspector";
import { newItemFor } from "./components/Inspector/ClipInspector";
import { Timeline } from "./components/Timeline/Timeline";
import { LibraryPanel } from "./components/Library/LibraryPanel";
import { PhoneShell } from "./components/Phone/PhoneShell";
import { AuthoredEditor } from "./AuthoredEditor";
import type { JSONSchema } from "./SchemaForm";
import { useHistory, type Props } from "./state/history";
import { useDebounced } from "./state/useDebounced";
import { useMediaQuery } from "./state/useMediaQuery";
import { useResizablePanel } from "./state/useResizablePanel";
import { errorIndex, validateProps } from "./state/validation";
import { applyTimeEdit, buildLanes, descriptorFor, CAPTION_POSITION_SECTION, type ClipItem } from "./state/model";
import { centeredPosition, NATIVE_W, NATIVE_H } from "./state/geometry";
import { buildLayerRows, type LayerClipItem } from "./state/layers";
import { computeOutputDuration, normalizeCuts, outputToSource, type VideoCut } from "../src/cuts";
import { FPS, cutsRef, frameRef, framesToMs } from "./state/playhead";
import { broadcastSelection } from "./state/selectionBridge";

/** Width threshold for the phone shell. Must match styles.css's own
 *  `@media (max-width: 860px)` breakpoint by hand — CSS and this JS string
 *  have no shared source of truth, so if one changes, change both (the
 *  touch-target sizing rules under that CSS breakpoint apply inside
 *  whichever shell is currently rendered; a mismatch would size things for
 *  the wrong shell right at the boundary). */
const PHONE_BREAKPOINT_QUERY = "(max-width: 860px)";

const schema = renderPropsSchema as JSONSchema;

// Matches Captions.tsx's own hardcoded fallback exactly (x=(1080-960)/2,
// width=960, y placed so a ~160px box's bottom edge lands at CAPTION_BOTTOM
// (90) from the bottom of a 1920 canvas) — injecting this the moment a job
// loads, rather than leaving captionPosition absent until the user drags it,
// is what makes the box visible/draggable/clickable at all (PreviewOverlay,
// hitTestAt, and the Inspector all need a real array entry at index 0 to
// operate on; see state/model.ts's CAPTION_POSITION_SECTION bypass). Numerically
// identical to the render component's own default, so this injection is a
// pure no-op until the user actually drags the box.
const CAPTION_POSITION_DEFAULT = { x: 60, y: 1670, width: 960 };

function withDefaultCaptionPosition(props: Props): Props {
  const existing = (props as Record<string, unknown>)[CAPTION_POSITION_SECTION];
  if (Array.isArray(existing) && existing.length > 0) return props;
  return { ...props, [CAPTION_POSITION_SECTION]: [{ ...CAPTION_POSITION_DEFAULT }] };
}

// chapterNav/rainbowBar are new editor-only position objects with no
// pipeline-authored counterpart at all (unlike captionPosition, which
// shadows a render-side default that's absent from most-but-not-all jobs —
// these are absent from EVERY job today). Same "inject the numeric default
// immediately so there's a real object to read/drag" reasoning as
// CAPTION_POSITION_DEFAULT above — the values here are the exact defaults
// ChapterNav.tsx/RainbowProgressBar.tsx already fall back to when the field
// is omitted, so this is a pure no-op render-wise until actually dragged.
const CHAPTER_NAV_POSITION_DEFAULT = { x: 0, y: 0, width: 1080 };
const RAINBOW_BAR_POSITION_DEFAULT = { x: 0, y: 1912, width: 1080, height: 8 };

function withDefaultObjectPosition(props: Props, section: string, fallback: Record<string, unknown>): Props {
  const existing = (props as Record<string, unknown>)[section];
  if (existing && typeof existing === "object" && !Array.isArray(existing)) return props;
  return { ...props, [section]: { ...fallback } };
}

// 跟 whatsapp_mvp/webhook.py 的 OM_EDITOR_SAVES_PER_HOUR 默认值保持一致——
// 服务端才是权威判断（这里只是在第一次 /status 轮询结果回来之前给个合理的
// 初始上限显示，不影响实际是否被拒绝）。
const DEFAULT_SAVES_PER_HOUR = 12;

// How long a freshly-added card starts out on the timeline — see
// handleAddItem's own comment for why this needs to be finite at all.
const DEFAULT_NEW_ITEM_DURATION_FRAMES = FPS * 5;

type LoadState = "loading" | "ready" | "error";
type SaveState = "idle" | "saving" | "rendering" | "done" | "failed";
// index: null selects a top-level OBJECT field (intro/outro/compliance/…),
// not an array item — see state/model.ts's itemAt() for the read side.
type Selection = { section: string; index: number | null } | null;

function useJobIdAndToken(): { jobId: string; token: string } {
  return useMemo(() => {
    const parts = window.location.pathname.split("/").filter(Boolean);
    const jobId = parts[parts.length - 1] || "";
    const token = new URLSearchParams(window.location.search).get("token") || "";
    return { jobId, token };
  }, []);
}

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

/** 桌面 vs 触屏——按能力检测，不是按屏幕宽度。拖拽在桌面窄窗口也该可用，
 * 触屏在大平板上也该被禁用；`hover: hover` + `pointer: fine` 才是真正的信号。 */
function detectDesktopPointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return true;
  return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
}

function blurActiveElement(): void {
  const el = document.activeElement as HTMLElement | null;
  // FrameInput / 文本输入都是 onBlur 才提交——撤销前必须先让它们提交，
  // 否则会出现"数值已经撤销了，但输入框还显示着刚才没提交完的文字"。
  el?.blur?.();
}

/**
 * 外层壳——只负责拿到 jobId/token、发出加载请求、显示 loading/error。
 *
 * 之所以拆成外层 App + 内层 Editor 两个组件，而不是在一个组件里用
 * `loadState==="loading"` 做提前 return：`useDebounced`（以及任何用
 * `useState(value)` 惰性初始化的 hook）只在它所在组件**第一次挂载**时读取
 * 初始值。如果这些 hook 跟 loading/error 分支活在同一个组件里，Rules of
 * Hooks 要求它们必须在提前 return 之前无条件调用——也就是说它们会在数据还
 * 没到位时（props 还是占位空对象）就先跑一次，等真数据到达时，
 * `useDebounced` 已经把那个空对象当成"初始值"锁死了，要再等一个防抖周期
 * 才会追上真实数据。这个窗口期一旦被 `<Player>` 撞上——inputProps
 * 里连 chapters/captions/scenes 这些必填字段都没有——合成组件会直接抛错
 * （真实复现过：interpolate() "must contain only numbers"、"Cannot read
 * properties of undefined"）。让 Editor 只在数据真正就绪后才挂载，就让
 * 它内部每一个 hook 的"第一次渲染"天然拿到的就是真实数据，从根上不存在
 * 这个窗口。
 */
export function App() {
  const { jobId, token } = useJobIdAndToken();
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [initial, setInitial] = useState<{ props: Props; jobStatus: string } | null>(null);
  const [isAuthored, setIsAuthored] = useState(false);

  const load = useCallback(async () => {
    setLoadState("loading");
    setLoadError(null);
    // Arm B (AI-authored) jobs have no render_props.schema.json-shaped props
    // at all — they only exist as `job_dir/authored/scene.tsx`. Probe the
    // authored route first; a 404 there means this is an ordinary Arm A job,
    // so fall through to the props route exactly as before. There's no DB
    // flag to check instead — arm detection is filesystem-based server-side
    // (see webhook.py's editor_get_authored), and this mirrors that same
    // "try authored, fall back to props" shape on the client. `AuthoredEditor`
    // does its own fetch of the same route once mounted — this call is only
    // used to decide WHICH component to mount, not threaded through as data.
    try {
      await apiGet(`/api/editor/${encodeURIComponent(jobId)}/authored`, token);
      setIsAuthored(true);
      setLoadState("ready");
      return;
    } catch {
      // Not an authored job (404) or the authored route failed — fall
      // through to the normal Arm A load below.
    }
    try {
      const data = await apiGet(`/api/editor/${encodeURIComponent(jobId)}/props`, token);
      setIsAuthored(false);
      let initialProps = withDefaultCaptionPosition(data.props);
      initialProps = withDefaultObjectPosition(initialProps, "chapterNav", CHAPTER_NAV_POSITION_DEFAULT);
      initialProps = withDefaultObjectPosition(initialProps, "rainbowBar", RAINBOW_BAR_POSITION_DEFAULT);
      setInitial({ props: initialProps, jobStatus: data.job_status || "" });
      setLoadState("ready");
    } catch (e) {
      setLoadError(String(e instanceof Error ? e.message : e));
      setLoadState("error");
    }
  }, [jobId, token]);

  useEffect(() => { load(); }, [load]);

  if (loadState === "ready" && isAuthored) {
    return <AuthoredEditor jobId={jobId} token={token} />;
  }

  if (loadState === "loading" || !initial) {
    return <div className="center-msg">Loading editor…</div>;
  }
  if (loadState === "error") {
    return (
      <div className="center-msg">
        <div>
          <div className="banner banner--error">Couldn't load this editor link: {loadError}</div>
          <button type="button" className="btn btn--primary" onClick={load}>Retry</button>
        </div>
      </div>
    );
  }

  return (
    <Editor
      jobId={jobId}
      token={token}
      initialProps={initial.props}
      initialJobStatus={initial.jobStatus}
    />
  );
}

function Editor({
  jobId, token, initialProps, initialJobStatus,
}: {
  jobId: string; token: string; initialProps: Props; initialJobStatus: string;
}) {
  const [jobStatus] = useState(initialJobStatus);

  const { props, commit, undo, redo, canUndo, canRedo } = useHistory(initialProps);

  const [selection, setSelection] = useState<Selection>(null);
  // Mirror selection into the Phase B overlay's own store rather than
  // passing it as a PreviewPane prop — see selectionBridge.ts's comment for
  // why (PreviewPane's memo depends on selection never reaching it as a prop).
  useEffect(() => { broadcastSelection(selection); }, [selection]);
  // 第一次挂载时 present === initialProps（同一个引用），所以这里可以直接
  // 拿 initialProps 当"已保存"的基准，不需要额外一次 setState 才能追上。
  const [savedSnapshot, setSavedSnapshot] = useState<Props>(initialProps);

  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savesThisHour, setSavesThisHour] = useState<number | null>(null);
  const [slotBusy, setSlotBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  const [relayoutBusy, setRelayoutBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);

  const [isPlaying, setIsPlaying] = useState(false);
  const isDesktopPointer = useMemo(detectDesktopPointer, []);

  // isPhone (width, live-subscribed) is a DIFFERENT signal from
  // isDesktopPointer (pointer capability, read once) — this is the actual
  // fix for a real bug the two being conflated caused: Timeline's row
  // height/label width used to follow capability while styles.css's own
  // sizing followed width, so a narrow desktop window and a large touch
  // tablet each disagreed with themselves. isPhone drives LAYOUT (which
  // shell); isDesktopPointer continues to drive INTERACTION (drag/gesture
  // enablement) — see Timeline.tsx's isTouch prop, still fed from the latter.
  // `?ui=phone`/`?ui=desktop` overrides it for testing on any device.
  const uiOverride = useMemo(() => new URLSearchParams(window.location.search).get("ui"), []);
  const widthIsPhone = useMediaQuery(PHONE_BREAKPOINT_QUERY);
  const isPhone = uiOverride === "phone" ? true : uiOverride === "desktop" ? false : widthIsPhone;

  // Video/Audio synthetic-lane imagery — generated once server-side (ffmpeg,
  // not decoded client-side) and cached per job; fetched once on mount
  // alongside the quota probe above, not tied to the props load/save cycle.
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

  const playerRef = useRef<PlayerRef>(null);

  // Drag-to-resize the timeline against the preview above it — a plain
  // ref, not conditioned on `isPhone`: this is a hook, so it must run
  // unconditionally regardless of which shell ends up rendering below;
  // useResizablePanel itself no-ops harmlessly while appRootRef.current is
  // null (i.e. whenever the phone shell is the one actually mounted).
  const appRootRef = useRef<HTMLDivElement>(null);
  const { handleProps: timelineResizeHandleProps } = useResizablePanel({
    storageKey: "om-editor-desktop-timeline-h",
    cssVar: "--timeline-h",
    targetRef: appRootRef,
    defaultPx: 260,
    minPx: 120,
    // Leaves at least 30% of the viewport for the preview above it.
    maxPx: () => Math.max(200, window.innerHeight * 0.7),
  });

  const showToast = useCallback((msg: string) => {
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = window.setTimeout(() => setToast(null), 5000);
  }, []);

  useEffect(() => () => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
  }, []);

  // 打开链接时先探一次配额，不用等第一次保存才知道还剩几次。
  useEffect(() => {
    apiGet(`/api/editor/${encodeURIComponent(jobId)}/status`, token)
      .then((s) => { setSavesThisHour(s.saves_this_hour ?? null); setSlotBusy(!!s.slot_busy); })
      .catch(() => {});
  }, [jobId, token]);

  const isDirty = props !== savedSnapshot;

  // 未保存的改动离开页面前提醒——之前完全没有这道保险。
  useEffect(() => {
    if (!isDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [isDirty]);

  // ── Derived: duration / cuts / debounced props for Player+ajv / lanes / errors ──
  // sourceDurationFrames is the full, untrimmed source length — durationSeconds
  // never changes when cuts are added (see src/cuts.ts's header comment: it
  // stays server-pinned, cuts are a separate list). durationInFrames is the
  // OUTPUT length the Player/timeline actually use — single source of truth
  // is computeOutputDuration (src/cuts.ts), shared with the composition
  // itself, so this can never drift the way three hand-duplicated copies of
  // this formula already did once this session.
  const sourceDurationFrames = useMemo(
    () => Math.max(1, Math.ceil((Number(props.durationSeconds) || 1) * FPS)),
    [props.durationSeconds]
  );
  const cuts = useMemo(
    () => normalizeCuts(props.videoCuts as VideoCut[] | undefined, sourceDurationFrames),
    [props.videoCuts, sourceDurationFrames]
  );
  const durationInFrames = useMemo(() => computeOutputDuration(props), [props]);

  // Keep the command-imperative cache (state/playhead.ts's cutsRef) in sync
  // for FrameInput's "jump to playhead" button — see that file's doc comment
  // on why this can't just be a prop threaded through the form tree.
  useEffect(() => { cutsRef.current = cuts; }, [cuts]);

  // 播放时用更长的防抖——见 useDebounced.ts 顶部注释：inputProps 每变一次，
  // Remotion 的播放循环就会被拆掉重建一次，播放途中要更保守。这里第一次
  // 渲染时 props 已经是真实数据（见上面 Editor 拆分的原因），所以
  // useDebounced 的惰性初始值天然就是对的，不会有空对象窗口期。
  const debouncedProps = useDebounced(props, isPlaying ? 400 : 150);

  const validation = useMemo(() => validateProps(debouncedProps), [debouncedProps]);
  const errIdx = useMemo(() => errorIndex(validation.errors), [validation.errors]);

  // lanes 用实时 props（不是防抖后的）：拖拽过程中的位置是直接写 DOM 的，
  // 不经过这里；lanes 只需要在"每次提交后"是准的，这本来就是正常的 React
  // 重渲染节奏，不需要额外防抖。
  const lanes = useMemo(() => buildLanes(props, durationInFrames, cuts), [props, durationInFrames, cuts]);
  const layerRows = useMemo(() => buildLayerRows(props, durationInFrames, cuts), [props, durationInFrames, cuts]);

  // ── Item mutations ──────────────────────────────────────────────────

  const handleFieldChange = useCallback((name: string, value: unknown) => {
    commit((prev) => ({ ...prev, [name]: value }), { coalesceKey: `field:${name}` });
  }, [commit]);

  const handleItemChange = useCallback((section: string, index: number | null, next: Record<string, unknown>) => {
    commit((prev) => {
      if (index === null) return { ...prev, [section]: next };
      const arr = Array.isArray(prev[section]) ? [...(prev[section] as unknown[])] : [];
      arr[index] = next;
      return { ...prev, [section]: arr };
    }, { coalesceKey: index === null ? `item:${section}` : `item:${section}:${index}` });
  }, [commit]);

  const handleAddItem = useCallback((section: string) => {
    const itemSchema = schema.properties?.[section]?.items;
    if (!itemSchema) return;
    const base = newItemFor(itemSchema);
    const d = descriptorFor(section);
    // frameRef is the Player's own playhead — OUTPUT-frame space. Every
    // item field written into props must be SOURCE-frame space (props are
    // never rebased when cuts change), so convert once here before use.
    const playFrame = outputToSource(frameRef.current, cuts);
    let withTime: Record<string, unknown> = base;
    if (d) {
      if (d.kind === "point") {
        withTime = { ...base, [d.frameField]: playFrame };
      } else if (d.kind === "rangeMs") {
        withTime = { ...base, [d.startField]: framesToMs(playFrame), [d.endField]: framesToMs(playFrame + DEFAULT_NEW_ITEM_DURATION_FRAMES) };
      } else {
        withTime = { ...base, [d.startField]: playFrame };
        // Always give a new card a real, finite end frame when the section's
        // schema has one at all (every mountFrame-based card type does —
        // `endRequired` only means the SCHEMA won't reject its absence, not
        // that leaving it off is a good default). Without this, a freshly
        // added card was open-ended by default and stretched all the way to
        // the end of the video — filling the rest of the timeline and
        // visibly shrinking/growing every time it was dragged, since an
        // open-ended clip's far edge is pinned to durationInFrames, not a
        // stored value (see useClipDrag.ts's own "openEnded" move branch).
        // Dragging the right edge afterward still works exactly as before
        // and can re-open it (Clip.tsx's resize-r), so nothing about that
        // deliberate, explicit path changes.
        if (d.endField) withTime[d.endField] = playFrame + DEFAULT_NEW_ITEM_DURATION_FRAMES;
      }
    }
    // newItemFor only ever populates schema-`required` fields, and no card's
    // x/y is required — without this the card renders (each component has
    // its own literal x/y default) but is invisible to the preview's
    // hit-test/drag overlay (estimatedRect hard-returns null without
    // numeric x/y), i.e. it's on screen but unselectable. Center it instead.
    const pos = centeredPosition(section, withTime);
    if (pos && typeof withTime.x !== "number") withTime = { ...withTime, ...pos };
    const currentArr = props[section];
    const newIndex = Array.isArray(currentArr) ? currentArr.length : 0;
    commit((prev) => {
      const arr = Array.isArray(prev[section]) ? [...(prev[section] as unknown[])] : [];
      arr.push(withTime);
      return { ...prev, [section]: arr };
    }, { coalesceKey: null });
    setSelection({ section, index: newIndex });
  }, [props, commit]);

  const handleDuplicateItem = useCallback((section: string, index: number) => {
    commit((prev) => {
      const arr = Array.isArray(prev[section]) ? [...(prev[section] as unknown[])] : [];
      const original = arr[index];
      const copy = original && typeof original === "object" ? JSON.parse(JSON.stringify(original)) : original;
      // A deep-copied x/y lands exactly on the original — nudge it so the
      // duplicate doesn't look like a no-op.
      if (copy && typeof copy === "object" && typeof copy.x === "number" && typeof copy.y === "number") {
        copy.x = Math.min(NATIVE_W - 1, copy.x + 40);
        copy.y = Math.min(NATIVE_H - 1, copy.y + 40);
      }
      arr.splice(index + 1, 0, copy);
      return { ...prev, [section]: arr };
    }, { coalesceKey: null });
    setSelection({ section, index: index + 1 });
  }, [commit]);

  const handleDeleteItem = useCallback((section: string, index: number) => {
    commit((prev) => {
      const arr = Array.isArray(prev[section]) ? [...(prev[section] as unknown[])] : [];
      arr.splice(index, 1);
      return { ...prev, [section]: arr };
    }, { coalesceKey: null });
    // 删除的项如果正好是当前选中项就清空选择；晚于它的同数组项要把下标减一
    // 跟上位移——否则用户接下来编辑的会是错误的那一项。sel.index===null can't
    // actually co-occur with a matching array section in practice (a given
    // section is either always-object or always-array), but guard it anyway
    // rather than lean on that invariant silently.
    setSelection((sel) => {
      if (!sel || sel.section !== section || sel.index === null) return sel;
      if (sel.index === index) return null;
      if (sel.index > index) return { section, index: sel.index - 1 };
      return sel;
    });
  }, [commit]);

  const handleTimeEdit = useCallback((clip: ClipItem, edit: { from?: number; to?: number }) => {
    commit((prev) => {
      const arr = Array.isArray(prev[clip.section]) ? [...(prev[clip.section] as unknown[])] : [];
      const item = arr[clip.index] as Record<string, unknown> | undefined;
      if (!item) return prev;
      arr[clip.index] = applyTimeEdit(clip.section, item, edit, durationInFrames, cuts);
      return { ...prev, [clip.section]: arr };
    }, { coalesceKey: null }); // 一次拖拽一条撤销记录，不是每个 pointermove 一条
  }, [commit, durationInFrames, cuts]);

  const handleLayerEdit = useCallback((clip: LayerClipItem, newLayer: number) => {
    commit((prev) => {
      const arr = Array.isArray(prev[clip.section]) ? [...(prev[clip.section] as unknown[])] : [];
      const item = arr[clip.index] as Record<string, unknown> | undefined;
      if (!item) return prev;
      arr[clip.index] = { ...item, layer: newLayer };
      return { ...prev, [clip.section]: arr };
    }, { coalesceKey: null }); // 一次拖拽一条撤销记录
  }, [commit]);

  // Cuts are just a data field now — no remap needed at all (every other
  // time-bearing field stays in SOURCE coordinates forever; src/cuts.ts
  // maps to OUTPUT at render/preview time, not at edit time). This is the
  // whole reason the non-destructive design was chosen: adding, adjusting,
  // or removing a cut is one field write, and nothing is ever lost.
  const handleCutsChange = useCallback((nextCuts: VideoCut[]) => {
    commit((prev) => ({ ...prev, videoCuts: nextCuts }), { coalesceKey: null });
  }, [commit]);

  const handleSeek = useCallback((frame: number) => {
    playerRef.current?.seekTo(frame);
  }, []);

  // Pause/resume playback around a playhead scrub (Timeline.tsx's
  // onScrubStart/onScrubEnd, via usePlayheadScrub.ts) — seekTo alone
  // doesn't pause, so without this the video keeps advancing under a
  // continuous drag instead of parking on the frame the user is dragging
  // to. Only resumes if it was ACTUALLY playing before the scrub began —
  // scrubbing while paused must leave it paused.
  const wasPlayingBeforeScrubRef = useRef(false);
  const handleScrubStart = useCallback(() => {
    wasPlayingBeforeScrubRef.current = playerRef.current?.isPlaying() ?? false;
    playerRef.current?.pause();
  }, []);
  const handleScrubEnd = useCallback(() => {
    if (wasPlayingBeforeScrubRef.current) playerRef.current?.play();
  }, []);

  // ── Save / Relayout ─────────────────────────────────────────────────

  const handleSave = useCallback(async () => {
    setSaveState("saving");
    setSaveError(null);
    const sentProps = props; // 捕获这一刻真正发送出去的内容，不是轮询结束时可能已经变了的 props
    try {
      await apiPost(`/api/editor/${encodeURIComponent(jobId)}/props`, token, sentProps);
      setSaveState("rendering");
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        try {
          const status = await apiGet(`/api/editor/${encodeURIComponent(jobId)}/status`, token);
          setSavesThisHour(status.saves_this_hour ?? null);
          setSlotBusy(!!status.slot_busy);
          if (status.state !== "rendering") {
            if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
            if (status.error) {
              setSaveState("failed");
              setSaveError(status.error);
            } else {
              setSaveState("done");
              // 服务端从不把 props 回传给我们（POST /props 只存
              // pending_props，不 echo）——干净来源就是我们自己发出去的那份。
              setSavedSnapshot(sentProps);
            }
          }
        } catch { /* 单次轮询失败先不当真，下一轮再看 —— 服务端可能只是短暂没响应 */ }
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
  }, [jobId, token, props]);

  const handleRelayout = useCallback(async () => {
    // Phase B made hand-dragging a card's x/y a first-class action — this
    // rewrites x/y for every content-zone card wholesale, silently
    // discarding any of that hand placement. A toast-plus-undo safety net
    // already existed for the relayout itself; this adds the missing
    // "are you sure" specifically because the thing at risk (manual drags)
    // didn't exist when that safety net was designed.
    if (!window.confirm("Auto-tidy will re-place every card's position — any cards you've dragged by hand will move. Continue?")) {
      return;
    }
    setRelayoutBusy(true);
    try {
      const result = await apiPost(`/api/editor/${encodeURIComponent(jobId)}/relayout`, token, props);
      commit(result.props, { coalesceKey: null });
      showToast("Layout replaced — Ctrl+Z to undo");
    } catch (e) {
      showToast(`Auto-tidy failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setRelayoutBusy(false);
    }
  }, [jobId, token, props, commit, showToast]);

  // ── Keyboard shortcuts ──────────────────────────────────────────────

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
      if (inTextInput) return; // 正在打字时不要抢 Space/方向键/Delete

      if (e.key === " ") { e.preventDefault(); playerRef.current?.toggle(); return; }
      if (e.key === "Escape") { setSelection(null); return; }
      if (
        (e.key === "Delete" || e.key === "Backspace") &&
        selection &&
        selection.index !== null &&
        selection.section !== CAPTION_POSITION_SECTION
      ) {
        e.preventDefault();
        handleDeleteItem(selection.section, selection.index);
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const dir = e.key === "ArrowLeft" ? -1 : 1;
        const step = e.shiftKey ? 10 : 1;
        playerRef.current?.seekTo(Math.max(0, frameRef.current + dir * step));
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [undo, redo, selection, handleDeleteItem]);

  // Shared between both shells — save-failure takes priority over the
  // auto-dismissing relayout toast so the two never show at once and fight
  // for the same strip of screen.
  const banner =
    saveState === "failed" && saveError ? (
      <div className="toast">
        {saveError}
        <button type="button" className="btn btn--sm btn--ghost" onClick={() => setSaveState("idle")}>✕</button>
      </div>
    ) : toast ? (
      <div className="toast">
        {toast}
        <button type="button" className="btn btn--sm btn--ghost" onClick={() => setToast(null)}>✕</button>
      </div>
    ) : null;

  if (isPhone) {
    return (
      <>
        <PhoneShell
          jobId={jobId}
          jobStatus={jobStatus}
          schema={schema}
          props={props}
          debouncedProps={debouncedProps}
          selection={selection}
          onSelect={setSelection}
          onFieldChange={handleFieldChange}
          onItemChange={handleItemChange}
          onAddItem={handleAddItem}
          onDuplicateItem={handleDuplicateItem}
          onDeleteItem={handleDeleteItem}
          errorForItem={(section, index) => (field) => errIdx.forField(section, index, field)}
          cuts={cuts}
          sourceDurationFrames={sourceDurationFrames}
          onCutsChange={handleCutsChange}
          isDirty={isDirty}
          canUndo={canUndo}
          canRedo={canRedo}
          onUndo={() => { blurActiveElement(); undo(); }}
          onRedo={() => { blurActiveElement(); redo(); }}
          onSave={handleSave}
          onRelayout={handleRelayout}
          saveState={saveState}
          relayoutBusy={relayoutBusy}
          savesThisHour={savesThisHour}
          savesPerHour={DEFAULT_SAVES_PER_HOUR}
          slotBusy={slotBusy}
          playerRef={playerRef}
          onPlayingChange={setIsPlaying}
          lanes={lanes}
          layerRows={layerRows}
          durationInFrames={durationInFrames}
          onSeek={handleSeek}
          onTimeEdit={handleTimeEdit}
          onLayerEdit={handleLayerEdit}
          hasItemError={(section, index) => errIdx.forItem(section, index)}
          filmstripUrls={filmstripUrls}
          waveformUrl={waveformUrl}
        />
        {banner}
      </>
    );
  }

  return (
    <div className="app" ref={appRootRef}>
      <Toolbar
        jobId={jobId}
        jobStatus={jobStatus}
        isDirty={isDirty}
        canUndo={canUndo}
        canRedo={canRedo}
        onUndo={() => { blurActiveElement(); undo(); }}
        onRedo={() => { blurActiveElement(); redo(); }}
        onSave={handleSave}
        onRelayout={handleRelayout}
        saveState={saveState}
        relayoutBusy={relayoutBusy}
        savesThisHour={savesThisHour}
        savesPerHour={DEFAULT_SAVES_PER_HOUR}
        slotBusy={slotBusy}
      />

      <LibraryPanel onAdd={handleAddItem} isTouch={!isDesktopPointer} />

      <div className="app__center">
        <PreviewPane
          props={debouncedProps as unknown as XiaojinEditorialProps}
          playerRef={playerRef}
          onPlayingChange={setIsPlaying}
          onItemChange={handleItemChange}
          onSelect={setSelection}
        />
      </div>

      <Inspector
        schema={schema}
        props={props}
        selection={selection}
        onSelectionChange={setSelection}
        onFieldChange={handleFieldChange}
        onItemChange={handleItemChange}
        onAddItem={handleAddItem}
        onDuplicateItem={handleDuplicateItem}
        onDeleteItem={handleDeleteItem}
        errorFor={(f) => errIdx.forSection(f)}
        errorForItem={(section, index) => (field) => errIdx.forField(section, index, field)}
        cuts={cuts}
        sourceDurationFrames={sourceDurationFrames}
        onCutsChange={handleCutsChange}
      />

      <div
        className="app__resize-h"
        title="Drag to resize the timeline"
        {...timelineResizeHandleProps}
      />

      <Timeline
        lanes={lanes}
        layerRows={layerRows}
        durationInFrames={durationInFrames}
        selection={selection}
        onSelect={setSelection}
        onSeek={handleSeek}
        onTimeEdit={handleTimeEdit}
        onLayerEdit={handleLayerEdit}
        hasItemError={(section, index) => errIdx.forItem(section, index)}
        isTouch={!isDesktopPointer}
        filmstripUrls={filmstripUrls}
        waveformUrl={waveformUrl}
        cuts={cuts}
        onScrubStart={handleScrubStart}
        onScrubEnd={handleScrubEnd}
      />

      {banner}
    </div>
  );
}
