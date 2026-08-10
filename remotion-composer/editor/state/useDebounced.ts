import { useEffect, useState } from "react";

/**
 * 防抖一个值。编辑器里有两个昂贵的下游消费者共用它：`<Player>` 的 inputProps
 * 和 ajv 校验。
 *
 * 为什么播放时要用更长的延迟：`inputProps` 会一路流进 Remotion 的
 * `compositionManagerContext`，产生一个新的 `config` 引用；而
 * `use-playback.js` 的 rAF 主循环把 `config` 列在依赖数组里——也就是说**播放
 * 途中每改一次 props，播放循环都会被拆掉重建**，它的 `startedTime` 基准和
 * `framesAdvanced` 累计都会被清零，累积的亚帧时间被丢弃，表现就是播放变慢、
 * 卡顿。位置本身是安全的（不会跳回 0、视频元素不会重建，已从
 * Player.js/use-lazy-component.js 源码确认），但流畅度不是。
 */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    if (delayMs <= 0) {
      setDebounced(value);
      return;
    }
    const t = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(t);
  }, [value, delayMs]);

  return debounced;
}
