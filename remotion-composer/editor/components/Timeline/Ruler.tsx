import React from "react";
import { FPS } from "../../state/playhead";

/** 刻度间隔（秒）——按缩放挑一个不至于挤成一团的档位。 */
function tickStepSeconds(pxPerFrame: number): number {
  const pxPerSecond = pxPerFrame * FPS;
  for (const step of [1, 2, 5, 10, 15, 30, 60]) {
    if (pxPerSecond * step >= 54) return step;
  }
  return 120;
}

export const Ruler = React.memo(function Ruler({
  durationInFrames,
  pxPerFrame,
  labelWidth,
}: {
  durationInFrames: number;
  pxPerFrame: number;
  labelWidth: number;
}) {
  const step = tickStepSeconds(pxPerFrame);
  const totalSeconds = durationInFrames / FPS;
  const ticks: number[] = [];
  for (let s = 0; s <= totalSeconds; s += step) ticks.push(s);

  return (
    <div className="ruler" style={{ width: labelWidth + durationInFrames * pxPerFrame }}>
      {ticks.map((s) => (
        <div key={s} className="ruler__tick" style={{ left: labelWidth + s * FPS * pxPerFrame }}>
          {formatTick(s)}
        </div>
      ))}
    </div>
  );
});

function formatTick(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}
