// Undo/redo — 快照栈（不是 command pattern）。
//
// 选快照的理由：props 只有几 KB，50 层快照远不到 1MB；而 command pattern 要
// 求每一种编辑都写一个逆操作，但这个编辑器的表单是 schema 驱动的泛型代码
// （SchemaForm 不知道自己在改什么字段），逐一写逆操作只会引入 bug，换不来
// 任何好处。
//
// `present` 直接取代原来的 `props` useState——只有一个事实来源，不是两份并行
// 状态（那样迟早会不同步）。

import { useCallback, useRef, useState } from "react";

export type Props = Record<string, unknown>;

const MAX_PAST = 50;
const COALESCE_WINDOW_MS = 600;

export type HistoryState = {
  past: Props[];
  present: Props;
  future: Props[];
};

export type CommitOptions = {
  /** 人类可读的操作名（给 toast/调试用）。 */
  label?: string;
  /**
   * 合并键。同一个键在 COALESCE_WINDOW_MS 内的连续提交会合并成一条历史记录。
   * 不合并的话，在文本框里打 20 个字符会产生 20 条撤销记录，Ctrl+Z 一次只
   * 退一个字符——用户会觉得撤销"坏了"。
   * 传 null 表示这一步必须独立成一条记录（拖拽、重排、relayout）。
   */
  coalesceKey?: string | null;
};

/**
 * `initial` 必须是这个 job 真实加载完成的 props——调用方（Editor 组件）只在
 * 数据到位之后才挂载，所以这里永远拿到的是这个 hook 实例第一次渲染时的
 * 真实值，不会有"先塞一个占位空对象、数据到了再补上"的窗口期。这一点很
 * 重要：`useDebounced` 同样用 `useState(value)` 惰性初始化，如果 App 组件
 * 无条件调用它（Rules of Hooks 要求如此）、而 props 一开始是占位空对象，
 * `<Player>` 会先拿到一份缺了 chapters/captions/scenes 等必填字段的 props
 * 渲染一次——这正是之前真实复现过的 crash（interpolate 报"must contain only
 * numbers"、"Cannot read properties of undefined"）。拆成外层 App（只管加载）
 * + 内层 Editor（只在数据就绪后才挂载）就从根上消除了这个窗口。
 */
export function useHistory(initial: Props) {
  const [state, setState] = useState<HistoryState>(() => ({
    past: [],
    present: initial,
    future: [],
  }));
  const lastCommit = useRef<{ key: string | null; at: number }>({ key: null, at: 0 });

  const commit = useCallback((next: Props | ((prev: Props) => Props), opts: CommitOptions = {}) => {
    const { coalesceKey = null } = opts;
    const now = Date.now();
    const prevCommit = lastCommit.current;
    const shouldCoalesce =
      coalesceKey !== null &&
      prevCommit.key === coalesceKey &&
      now - prevCommit.at < COALESCE_WINDOW_MS;

    setState((s) => {
      const resolved = typeof next === "function" ? (next as (p: Props) => Props)(s.present) : next;
      if (resolved === s.present) return s;
      if (shouldCoalesce) {
        // 同一个字段的连续输入：替换 present，不往 past 里再压一层。
        return { past: s.past, present: resolved, future: [] };
      }
      const past = [...s.past, s.present];
      return {
        past: past.length > MAX_PAST ? past.slice(past.length - MAX_PAST) : past,
        present: resolved,
        future: [],
      };
    });
    lastCommit.current = { key: coalesceKey, at: now };
  }, []);

  const undo = useCallback(() => {
    setState((s) => {
      if (s.past.length === 0) return s;
      const previous = s.past[s.past.length - 1];
      return {
        past: s.past.slice(0, -1),
        present: previous,
        future: [s.present, ...s.future],
      };
    });
    // 撤销之后不允许再跟之前的输入合并，否则下一次输入会被并进已经被撤销的那条。
    lastCommit.current = { key: null, at: 0 };
  }, []);

  const redo = useCallback(() => {
    setState((s) => {
      if (s.future.length === 0) return s;
      const next = s.future[0];
      return {
        past: [...s.past, s.present],
        present: next,
        future: s.future.slice(1),
      };
    });
    lastCommit.current = { key: null, at: 0 };
  }, []);

  return {
    props: state.present,
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
    commit,
    undo,
    redo,
  };
}
