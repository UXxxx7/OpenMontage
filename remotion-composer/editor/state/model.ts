// 时间轴数据模型——**从 schema 推导**，不是手写 29 条表。
//
// contracts/render_props.schema.json 已经把每种卡片的时间字段声明清楚了，
// 手抄一份进 UI 代码只会在 pipeline 新增卡片类型时悄悄过期。这里在模块加载
// 时扫一遍 schema，为每个顶层数组字段推导出它的"时间形状"。

import renderPropsSchema from "../../../contracts/render_props.schema.json";
import { sourceToOutput, outputToSource, type VideoCut } from "../../src/cuts";
import { colorForSection, humanLabel, sectionGroupFor } from "../fieldHints";
import { FPS, msToFrames } from "./playhead";

const NO_CUTS: readonly VideoCut[] = [];

/** The one section that's a position, not a timed clip — always present/
 *  selectable regardless of frame, so it deliberately has no TimeDescriptor
 *  (deriveDescriptor naturally excludes it: its item schema has none of the
 *  time fields below, so it never gets a Timeline lane either). See
 *  itemTimeRange's own bypass for this section. */
export const CAPTION_POSITION_SECTION = "captionPosition";

type RawSchema = {
  properties?: Record<string, any>;
  required?: string[];
};

const schema = renderPropsSchema as RawSchema;

export type TimeDescriptor =
  /** mountFrame(+可选 endFrame) 或 fromFrame+toFrame */
  | { kind: "range"; startField: string; endField: string | null; endRequired: boolean }
  /** captions：整数毫秒 */
  | { kind: "rangeMs"; startField: string; endField: string }
  /** chapters/scenes/opacityKeyframes：只有一个时间点，没有时长 */
  | { kind: "point"; frameField: string };

/** 推导单个数组字段的时间形状；返回 null 表示它不该出现在时间轴上。 */
function deriveDescriptor(itemSchema: any): TimeDescriptor | null {
  const props = itemSchema?.properties;
  if (!props) return null;
  const required: string[] = itemSchema.required || [];

  if (props.startMs && props.endMs) {
    return { kind: "rangeMs", startField: "startMs", endField: "endMs" };
  }
  if (props.fromFrame && props.toFrame) {
    return {
      kind: "range",
      startField: "fromFrame",
      endField: "toFrame",
      endRequired: required.includes("toFrame"),
    };
  }
  if (props.mountFrame) {
    // endFrame 是可选的——省略时卡片会一直留在屏幕上直到片尾（组件里是
    // `if (endFrame !== undefined && frame >= endFrame) return null`，25 个
    // 组件里只有 1 个给了默认值）。这个 endRequired:false 是时间轴能正确
    // 画出"开放式"clip 的关键。
    return {
      kind: "range",
      startField: "mountFrame",
      endField: props.endFrame ? "endFrame" : null,
      endRequired: required.includes("endFrame"),
    };
  }
  if (props.atFrame) return { kind: "point", frameField: "atFrame" };
  if (props.frame) return { kind: "point", frameField: "frame" };
  return null;
}

/** section 名 -> 时间形状。模块加载时算一次。 */
export const TIME_DESCRIPTORS: Record<string, TimeDescriptor> = (() => {
  const out: Record<string, TimeDescriptor> = {};
  for (const [name, spec] of Object.entries(schema.properties || {})) {
    if (spec?.type !== "array" || !spec.items) continue;
    const d = deriveDescriptor(spec.items);
    if (d) out[name] = d;
  }
  return out;
})();

export function descriptorFor(section: string): TimeDescriptor | undefined {
  return TIME_DESCRIPTORS[section];
}

export function canResize(section: string): boolean {
  const d = descriptorFor(section);
  if (!d) return false;
  if (d.kind === "point") return false;
  if (d.kind === "rangeMs") return true;
  return true; // range: 右边缘可拖；endField 为 null 时拖动会"具现化" endFrame
}

/**
 * Read the item a Selection points at — `index: null` means `section` is a
 * top-level OBJECT (intro/outro/compliance/…), `index: number` means it's
 * an array item, same convention throughout the editor's Selection/HitTarget
 * types. Returns undefined if the shape doesn't match (e.g. index is a
 * number but props[section] isn't actually an array) rather than throwing —
 * callers already treat "no item" as "fall back to ProjectInspector".
 */
export function itemAt(
  props: Record<string, unknown>,
  section: string,
  index: number | null
): Record<string, unknown> | undefined {
  if (index === null) {
    const obj = props[section];
    return obj && typeof obj === "object" && !Array.isArray(obj) ? (obj as Record<string, unknown>) : undefined;
  }
  const arr = props[section];
  return Array.isArray(arr) ? (arr[index] as Record<string, unknown> | undefined) : undefined;
}

// ── Clip / lane 模型 ────────────────────────────────────────────────────

export type ClipItem = {
  section: string;
  index: number;
  /** 起点（帧） */
  from: number;
  /** 终点（帧）。开放式 clip 这里是片尾。 */
  to: number;
  /** 没有显式结束时间 —— 实际会一直显示到片尾 */
  openEnded: boolean;
  isPoint: boolean;
  label: string;
  row: number;
};

export type Lane = {
  section: string;
  label: string;
  group: string;
  color: string;
  items: ClipItem[];
  rows: number;
};

const LABEL_KEYS = ["title", "label", "headline", "text", "heading", "name", "question"];

function labelFor(item: Record<string, unknown>, section: string): string {
  for (const k of LABEL_KEYS) {
    const v = item[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  // rows[0].label 是 dataCards 这类"标题在子项里"的常见形状
  const rows = item.rows;
  if (Array.isArray(rows) && rows.length > 0 && typeof rows[0] === "object" && rows[0]) {
    const rl = (rows[0] as Record<string, unknown>).label;
    if (typeof rl === "string" && rl.trim()) return rl.trim();
  }
  return humanLabel(section);
}

/**
 * 读出某一项的时间范围，**换算成 OUTPUT 帧**（时间轴的坐标系——见
 * trimRemap.ts 的替代者 src/cuts.ts 顶部注释："Timeline axis stays in
 * OUTPUT frames"）。`item` 里存的原始字段值本身永远是 SOURCE 坐标（跟
 * props 落盘的坐标系一致，不随 cut 改变），这里用 `cuts` 做一次性换算，
 * 换算只发生在"要显示"这一刻，不改 item 本身。`cuts` 缺省是空数组，
 * 换算退化成恒等函数——没有 cuts 的旧 job 行为完全不变。
 *
 * 返回 null 表示这项没有可用的时间信息。
 */
export function itemTimeRange(
  section: string,
  item: Record<string, unknown>,
  durationInFrames: number,
  cuts: readonly VideoCut[] = NO_CUTS
): { from: number; to: number; openEnded: boolean; isPoint: boolean } | null {
  if (section === CAPTION_POSITION_SECTION) {
    // Not time-bound at all — always visible/selectable/clickable, same as
    // Arm B's own caption position box. Both hitTestAt and PreviewOverlay's
    // `visible` check go through this one function, so a single bypass here
    // covers both rather than duplicating the special case in each caller.
    return { from: 0, to: durationInFrames, openEnded: true, isPoint: false };
  }

  const d = descriptorFor(section);
  if (!d) return null;

  if (d.kind === "point") {
    const f = item[d.frameField];
    if (typeof f !== "number") return null;
    const mapped = sourceToOutput(f, cuts);
    return { from: mapped, to: mapped, openEnded: false, isPoint: true };
  }

  if (d.kind === "rangeMs") {
    const s = item[d.startField];
    const e = item[d.endField];
    if (typeof s !== "number") return null;
    const fromSrc = msToFrames(s);
    const toSrc = typeof e === "number" ? msToFrames(e) : fromSrc + 1;
    const from = sourceToOutput(fromSrc, cuts);
    const to = sourceToOutput(Math.max(toSrc, fromSrc + 1), cuts);
    return { from, to: Math.max(to, from + 1), openEnded: false, isPoint: false };
  }

  const s = item[d.startField];
  if (typeof s !== "number") return null;
  const from = sourceToOutput(s, cuts);
  const rawEnd = d.endField ? item[d.endField] : undefined;
  if (typeof rawEnd === "number") {
    const to = sourceToOutput(Math.max(rawEnd, s + 1), cuts);
    return { from, to: Math.max(to, from + 1), openEnded: false, isPoint: false };
  }
  // 没有结束帧 = 一直显示到片尾。旧时间轴在这里画 `from + 30`（1 秒），
  // 于是用户完全看不到卡片其实堆到了片尾——那是"UI 看起来很乱"的一大来源。
  return { from, to: durationInFrames, openEnded: true, isPoint: false };
}

/** 贪心区间打包：把互不重叠的 clip 塞进同一行。 */
function packRows(items: ClipItem[], minGapFrames: number): number {
  const rowEnds: number[] = [];
  const sorted = [...items].sort((a, b) => a.from - b.from);
  for (const it of sorted) {
    let placed = false;
    for (let r = 0; r < rowEnds.length; r++) {
      if (it.from >= rowEnds[r] + minGapFrames) {
        it.row = r;
        rowEnds[r] = it.to;
        placed = true;
        break;
      }
    }
    if (!placed) {
      it.row = rowEnds.length;
      rowEnds.push(it.to);
    }
  }
  return Math.max(1, rowEnds.length);
}

/**
 * 构建时间轴的 lane 列表。
 *
 * 一个数组字段 = 一条 lane（不是一个 SECTION_GROUP = 一条 lane）：把
 * "Data & numbers" 底下 9 个不相干的数组打包进同几行，会毁掉"我现在在编辑
 * 哪个数组"的心智模型，而且改动其中一个数组会让其它数组的行号乱跳。
 */
export function buildLanes(
  props: Record<string, unknown>,
  durationInFrames: number,
  cuts: readonly VideoCut[] = NO_CUTS
): Lane[] {
  const lanes: Lane[] = [];
  for (const section of Object.keys(TIME_DESCRIPTORS)) {
    const raw = props[section];
    if (!Array.isArray(raw) || raw.length === 0) continue;

    const items: ClipItem[] = [];
    raw.forEach((entry, index) => {
      if (typeof entry !== "object" || entry === null) return;
      const rec = entry as Record<string, unknown>;
      const range = itemTimeRange(section, rec, durationInFrames, cuts);
      if (!range) return;
      items.push({
        section,
        index,
        from: range.from,
        to: range.to,
        openEnded: range.openEnded,
        isPoint: range.isPoint,
        label: labelFor(rec, section),
        row: 0,
      });
    });
    if (items.length === 0) continue;

    let rows: number;
    if (section === "captions") {
      // 字幕是一条顺序轨道：本来就不该重叠，硬打包也只会得到一行；而万一
      // 真的重叠了，那是一个 bug，用户需要**看见**它，打包反而会藏起来。
      items.forEach((i) => { i.row = 0; });
      rows = 1;
    } else {
      rows = packRows(items, 1);
    }

    lanes.push({
      section,
      label: humanLabel(section),
      group: sectionGroupFor(section),
      color: colorForSection(section),
      items,
      rows,
    });
  }

  // 按 SECTION_GROUPS 的声明顺序分组排列，让相关的轨道挨在一起。
  const groupOrder = ["Structure", "Captions", "Data & numbers", "Cards & callouts", "Lists", "Other"];
  lanes.sort((a, b) => {
    const ga = groupOrder.indexOf(a.group);
    const gb = groupOrder.indexOf(b.group);
    if (ga !== gb) return ga - gb;
    return a.section.localeCompare(b.section);
  });
  return lanes;
}

// ── 写回：把新的时间范围写进 props ─────────────────────────────────────

export type TimeEdit = { from?: number; to?: number };

/**
 * 生成一项的时间字段更新（不修改入参）。
 *
 * captions 的毫秒陷阱：1000/30 = 33.333ms，而 startMs/endMs 是整数、且
 * `additionalProperties:false` 不允许在项上缓存帧号——所以 ms→frame→ms 不是
 * 恒等变换，反复拖动会累积漂移。调用方必须始终基于**拖拽起点的原始值**计算
 * （不要在上一次结果上累加），并且在帧号没变时不写入。
 */
export function applyTimeEdit(
  section: string,
  item: Record<string, unknown>,
  edit: TimeEdit,
  durationInFrames: number,
  cuts: readonly VideoCut[] = NO_CUTS
): Record<string, unknown> {
  const d = descriptorFor(section);
  if (!d) return item;
  const next = { ...item };

  // `edit.from`/`edit.to` arrive in OUTPUT frames (the timeline's own
  // coordinate system — see itemTimeRange's doc comment) since that's what
  // the drag/UI layer operates in; item fields on disk stay SOURCE forever.
  // Clamp in OUTPUT space (against the visible timeline's own bounds), then
  // convert to SOURCE only at the point of writing. With no cuts,
  // outputToSource is the identity function — behavior is unchanged.
  const clampOutputFrame = (f: number) => Math.max(0, Math.min(Math.round(f), durationInFrames));
  const toSourceFrame = (outputFrame: number) => outputToSource(clampOutputFrame(outputFrame), cuts);

  if (d.kind === "point") {
    if (edit.from !== undefined) next[d.frameField] = toSourceFrame(edit.from);
    return next;
  }

  if (d.kind === "rangeMs") {
    if (edit.from !== undefined) {
      next[d.startField] = Math.max(0, Math.round((toSourceFrame(edit.from) / FPS) * 1000));
    }
    if (edit.to !== undefined) {
      next[d.endField] = Math.max(0, Math.round((toSourceFrame(edit.to) / FPS) * 1000));
    }
    // schema 不强制 end > start，塌缩成 0 长度的字幕是一个静默的渲染 bug。
    const s = Number(next[d.startField]) || 0;
    const e = Number(next[d.endField]) || 0;
    if (e <= s) next[d.endField] = s + 1;
    return next;
  }

  if (edit.from !== undefined) next[d.startField] = toSourceFrame(edit.from);
  if (edit.to !== undefined && d.endField) {
    // 拖右边缘会把"开放式"变成显式结束帧——这是一个真实的语义改变
    // （从"一直显示到片尾"变成"到这里消失"），UI 上要能看出来。
    const start = Number(next[d.startField]) || 0;
    next[d.endField] = Math.max(toSourceFrame(edit.to), start + 1);
  }
  return next;
}

// A whole-item shift (drag the clip body, not an edge) is NOT done by
// recomputing a delta and re-deriving the item's current position here —
// useClipDrag.ts already computes the exact snapped/clamped ABSOLUTE
// from/to in OUTPUT space during the gesture, and hands that straight to
// applyTimeEdit via handleTimeEdit. A second implementation that instead
// took a delta and re-read item[startField] through cuts (this function
// used to be that) converts the same quantity twice, independently — the
// exact round-trip drift applyTimeEdit's own doc comment warns about for
// ms<->frame captions. One implementation, one call site.
//
// The behavior that implementation existed to protect — open-ended clips
// (`endField` absent) must stay open on a plain move, only an explicit
// resize of the right edge may materialize an endFrame — is enforced at
// the call site instead: see useClipDrag.ts's `finish()`, `keepOpen`.
