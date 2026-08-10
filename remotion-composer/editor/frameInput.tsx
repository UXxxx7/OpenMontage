import React, { useEffect, useState } from "react";
import { outputToSource } from "../src/cuts";
import { FPS, cutsRef, frameRef, framesToTimecode, timecodeToFrames } from "./state/playhead";

/**
 * 时间码输入 + 微调 + "跳到播放头"。
 *
 * 微调按钮不是可有可无的装饰：手机上一段 33 秒的视频横向只有约 350px，
 * 一个卡片 clip 只有几 px 宽，根本拖不准；桌面端要精确到帧时拖拽也不如点
 * 按钮。这一排按钮在触屏和桌面都能用，也是"如果拖拽功能出问题，编辑器依然
 * 完全可用"的保底。
 */
export function FrameInput({
  value,
  onChange,
  invalid,
  compact,
}: {
  value: number;
  onChange: (v: number) => void;
  invalid?: boolean;
  compact?: boolean;
}) {
  const [text, setText] = useState(() => framesToTimecode(value || 0));

  // 外部值变化（撤销、拖拽提交、跳到播放头）时同步回输入框。
  useEffect(() => {
    setText(framesToTimecode(value || 0));
  }, [value]);

  const commitText = () => {
    const frames = timecodeToFrames(text);
    if (frames !== null && frames >= 0) {
      if (frames !== value) onChange(frames);
    } else {
      setText(framesToTimecode(value || 0)); // 解析不了就还原，绝不静默写坏数据
    }
  };

  const nudge = (delta: number) => {
    onChange(Math.max(0, (value || 0) + delta));
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
        <input
          className={`input input--time${invalid ? " input--invalid" : ""}`}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={commitText}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.currentTarget.blur(); }
            if (e.key === "Escape") { setText(framesToTimecode(value || 0)); e.currentTarget.blur(); }
          }}
          aria-label="Timecode (mm:ss.ff)"
        />
        <button
          type="button"
          className="btn btn--sm"
          title="Set to current playhead position"
          // frameRef is the Player's own playhead (OUTPUT-frame space); every
          // field this component ever writes into (props are never rebased
          // by cuts) is SOURCE-frame space. Equal until a cut exists — must
          // convert or this silently writes the wrong frame once one does.
          onClick={() => onChange(Math.max(0, outputToSource(frameRef.current, cutsRef.current)))}
        >
          ⦿
        </button>
      </div>
      {!compact && (
        <div className="nudge">
          <button type="button" className="btn btn--sm" title="Back 1 second" onClick={() => nudge(-FPS)}>−1s</button>
          <button type="button" className="btn btn--sm" title="Back 1 frame" onClick={() => nudge(-1)}>−1f</button>
          <button type="button" className="btn btn--sm" title="Forward 1 frame" onClick={() => nudge(1)}>+1f</button>
          <button type="button" className="btn btn--sm" title="Forward 1 second" onClick={() => nudge(FPS)}>+1s</button>
          <span className="field__hint" style={{ marginLeft: 2 }}>frame {value ?? 0}</span>
        </div>
      )}
    </div>
  );
}

/** 毫秒版（captions 专用）——UI 上仍然显示时间码，写回时换算成整数毫秒。 */
export function MsInput({
  value,
  onChange,
  invalid,
}: {
  value: number;
  onChange: (ms: number) => void;
  invalid?: boolean;
}) {
  const frames = Math.round(((value || 0) / 1000) * FPS);
  return (
    <FrameInput
      value={frames}
      invalid={invalid}
      onChange={(f) => onChange(Math.max(0, Math.round((f / FPS) * 1000)))}
    />
  );
}
