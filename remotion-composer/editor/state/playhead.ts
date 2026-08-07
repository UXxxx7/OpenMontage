import type { VideoCut } from "../../src/cuts";

// 播放头位置——刻意不放进 React state。
//
// `frameupdate` 在播放时每帧都触发（30fps）。如果播放头帧号是 React state，
// 那么每一帧都会重渲染订阅它的整棵子树；叠加上这个编辑器本来就有的开销
// （每次 props 变化都要跑一遍 1604 行 schema 的 ajv 校验 + 重渲染整个
// XiaojinEditorial 合成——那里面 26 个数组 map 出约 40 个卡片组件，且没有任何
// React.memo），播放会明显卡顿。
//
// 所以：帧号存在这个模块级 store 里，播放头和时间码用**直接写 DOM**的方式
// 更新（style.transform / textContent），每帧的 React 工作量是 0。

type Listener = (frame: number) => void;

const listeners = new Set<Listener>();

/** 同步读取当前帧——给"在播放头位置加一张卡"这类命令式操作用。这是 Player
 * 自己的播放头，永远是 OUTPUT-frame 空间。 */
export const frameRef = { current: 0 };

/**
 * App.tsx 每次 cuts 变化时同步写入——纯缓存，不是数据源（数据源是 props.
 * videoCuts，见 src/cuts.ts）。存在的唯一理由：FrameInput 的"跳到播放头"按钮
 * (⦿) 写入的是某个 item 字段，而所有 item 字段都是 SOURCE-frame 空间，跟
 * frameRef 的 OUTPUT-frame 空间不是一回事——两者在没有 cuts 时刚好相等，
 * cuts 一出现就会分叉。没有这份缓存，FrameInput/SchemaForm 就得把 cuts 一路
 * 从 App.tsx 传穿整棵表单树（ObjectField → SchemaField → FrameInput），
 * 而 cuts 变化的频率和播放头一样是"命令式读取"场景，不是渲染输入。 */
export const cutsRef: { current: readonly VideoCut[] } = { current: [] };

export function setPlayheadFrame(frame: number): void {
  if (frame === frameRef.current) return;
  frameRef.current = frame;
  // 同步调用：这些回调都是直接写 DOM 的，不能等 React 调度。
  listeners.forEach((l) => l(frame));
}

/**
 * 订阅每一帧。回调里**只应该做直接 DOM 写入**，绝不要 setState——那正是这个
 * store 存在的意义。返回取消订阅函数。
 */
export function subscribePlayhead(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function resetPlayhead(): void {
  frameRef.current = 0;
}

// ── 时间码格式化（整个编辑器共用一套，避免各处小数位/补零不一致）─────────

export const FPS = 30;

export function framesToTimecode(frames: number): string {
  const safe = Math.max(0, Math.round(frames));
  const totalSeconds = Math.floor(safe / FPS);
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  const ff = safe % FPS;
  return `${mins}:${String(secs).padStart(2, "0")}.${String(ff).padStart(2, "0")}`;
}

export function timecodeToFrames(tc: string): number | null {
  const trimmed = tc.trim();
  const m = trimmed.match(/^(\d+):(\d{1,2})(?:[.:](\d{1,2}))?$/);
  if (!m) {
    // 也接受直接输入帧号——高级用户经常想精确到帧。
    const asNumber = Number(trimmed);
    return Number.isFinite(asNumber) && asNumber >= 0 ? Math.round(asNumber) : null;
  }
  const mins = Number(m[1]);
  const secs = Number(m[2]);
  const ff = Number(m[3] || 0);
  if (secs >= 60 || ff >= FPS) return null;
  return Math.round((mins * 60 + secs) * FPS + ff);
}

export function msToFrames(ms: number): number {
  return Math.round((ms / 1000) * FPS);
}

export function framesToMs(frames: number): number {
  return Math.round((frames / FPS) * 1000);
}
