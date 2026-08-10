// 字段展示顺序/折叠策略 + 分组配色——不是 contracts/render_props.schema.json
// 的一部分，纯 UI 层面的取舍。核心内容字段（标题/正文/挂载时间）排最前面，
// 几何/坐标字段收进"高级"折叠区，减少表单第一眼的认知负担——26 种卡片类型
// 共用同一套规则，不需要为每种类型单独写展示逻辑。

import { isDraggableSection } from "./state/positioning";

const PRIORITY_FIELDS = [
  "title", "label", "labelEn", "headline", "text", "value", "unitLabel",
  "leftLabel", "rightLabel", "prosLabel", "consLabel", "pros", "cons",
  "mountFrame", "endFrame", "fromFrame", "toFrame", "atFrame", "frame",
  "startMs", "endMs",
];

const ADVANCED_FIELDS = new Set([
  "x", "y", "width", "height", "w", "h", "radius",
  "objectPosition", "tone", "icon",
]);

// 这几个字段客户端编辑了也不会生效——render_props_directly 的
// pin_server_owned_props 会在保存时强制换成服务端磁盘上的真实值（防
// SSRF/任意文件读取/时长 DoS）。表单里显示成只读，而不是让用户改了半天
// 才发现根本没用上。
export const SERVER_PINNED_TOP_LEVEL = new Set(["videoSrc", "durationSeconds"]);
export const SERVER_PINNED_NESTED: Record<string, string> = {
  presenter: "src",
  qrContact: "qrSrc",
};

export function fieldPriority(name: string): number {
  const idx = PRIORITY_FIELDS.indexOf(name);
  return idx === -1 ? PRIORITY_FIELDS.length : idx;
}

export function isAdvancedField(name: string, section?: string): boolean {
  // Phase B: sections with plain draggable x/y/width no longer need those
  // three fields hidden behind the fold — the user can now see and adjust
  // the exact numbers for the very thing they just dragged, right in the
  // main form, instead of behind an extra click.
  if ((name === "x" || name === "y" || name === "width") && section && isDraggableSection(section)) {
    return false;
  }
  return ADVANCED_FIELDS.has(name);
}

export function sortFieldNames(names: string[]): string[] {
  return [...names].sort((a, b) => fieldPriority(a) - fieldPriority(b));
}

// 帧字段——绑定 FrameInput 那个 mm:ss.ff <-> frame 双向转换小部件。
// 必须同时匹配小写的裸 `frame`（scenes[].frame / opacityKeyframes[].frame）
// 和驼峰的 `mountFrame`/`endFrame`/`atFrame` 等；保留 `$` 结尾锚点是有意的，
// 它正确地把 revealFrames / fillDelayFrames / fillDurationFrames 排除在外
// ——那些是"持续多少帧"（时长），不是"在第几帧"（位置），套时间码控件是错的。
export function isFrameField(name: string): boolean {
  return /^(frame|.*Frame)$/.test(name);
}

// 毫秒字段（目前只有 captions 的 startMs/endMs）。schema 把它们定义成整数
// 毫秒，不是帧——时间轴显示要换算，写回也要换算，见 state/model.ts。
export function isMsField(name: string): boolean {
  return /Ms$/.test(name);
}

// 顶层数组字段的分组：侧栏/时间轴按用途分组，而不是按 schema 声明顺序平铺
// 一长串。每组一个颜色，让时间轴上的 clip 不用读文字就知道是什么类型。
export const SECTION_GROUPS: Record<string, string[]> = {
  "Structure": ["chapters", "scenes", "opacityKeyframes", "sections"],
  "Captions": ["captions"],
  "Data & numbers": ["dataCards", "gauges", "countdowns", "calendarEvents", "beforeAfter", "barCharts", "progressBars", "milestoneTracks", "milestoneUnlocks"],
  "Cards & callouts": ["topicCards", "cornerCards", "quotes", "testimonials", "trustBadges", "pills", "zoneHeaders"],
  "Lists": ["stepLists", "rankedLists", "checklists", "prosCons", "comparisons", "iconClusters", "locationPins"],
};

export const GROUP_COLORS: Record<string, string> = {
  "Structure": "var(--lane-structure)",
  "Captions": "var(--lane-captions)",
  "Data & numbers": "var(--lane-data)",
  "Cards & callouts": "var(--lane-cards)",
  "Lists": "var(--lane-lists)",
  "Other": "var(--lane-other)",
};

export function sectionGroupFor(fieldName: string): string {
  for (const [group, fields] of Object.entries(SECTION_GROUPS)) {
    if (fields.includes(fieldName)) return group;
  }
  return "Other";
}

export function colorForSection(fieldName: string): string {
  return GROUP_COLORS[sectionGroupFor(fieldName)] || GROUP_COLORS.Other;
}

// 顶层非数组字段的分区（项目设置面板用）。
export const BASIC_SCALAR_FIELDS = [
  "colorMode", "speakerObjectPosition", "introOutFrame", "headingFont", "labelFont",
];
export const OBJECT_GROUP_FIELDS = [
  "presenter", "qrContact", "intro", "outro", "compliance", "brand",
];

// 人话字段名——schema 的 key 是给机器看的（`prosCons`/`zoneHeaders`），
// 直接摆在 UI 上对用户不友好。没列进来的走一个通用的 camelCase 拆分。
const LABEL_OVERRIDES: Record<string, string> = {
  captionPosition: "Caption position",
  dataCards: "Data cards",
  beforeAfter: "Before / after",
  calendarEvents: "Calendar",
  barCharts: "Bar charts",
  progressBars: "Progress bars",
  milestoneTracks: "Milestones",
  milestoneUnlocks: "Unlocks",
  topicCards: "Topic cards",
  cornerCards: "Corner cards",
  trustBadges: "Trust badges",
  zoneHeaders: "Zone headers",
  stepLists: "Steps",
  rankedLists: "Ranked lists",
  prosCons: "Pros & cons",
  iconClusters: "Icons",
  locationPins: "Locations",
  opacityKeyframes: "Facecam opacity",
  scenes: "Facecam layout",
  qrContact: "QR / contact",
  colorMode: "Colour mode",
  speakerObjectPosition: "Speaker framing",
  introOutFrame: "Intro ends at",
  headingFont: "Heading font",
  labelFont: "Label font",
};

export function humanLabel(name: string): string {
  if (LABEL_OVERRIDES[name]) return LABEL_OVERRIDES[name];
  const spaced = name.replace(/([a-z0-9])([A-Z])/g, "$1 $2");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

// 单数形式——inspector 标题显示"Data card"而不是"Data cards"。
export function singularLabel(name: string): string {
  const l = humanLabel(name);
  if (/ies$/.test(l)) return l.replace(/ies$/, "y");
  if (/s$/.test(l) && !/ss$/.test(l)) return l.replace(/s$/, "");
  return l;
}
