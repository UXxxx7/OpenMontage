import React from "react";

/**
 * The synthetic Video + Audio reference lanes pinned above the schema/
 * manifest-driven lanes — shared between Arm A (Timeline.tsx, extracted
 * from its own former inline JSX) and Arm B (Authored/AuthoredTimeline.tsx,
 * which never had these lanes at all). Always read-only: cuts are made via
 * the Inspector's buttons (Arm A) or don't exist at all (Arm B has no cuts
 * model), never by dragging here.
 *
 * `spliceFrames` is OUTPUT-frame splice-marker positions. Arm A derives
 * these from its own `cuts` model via `sourceToOutput` and passes them in;
 * Arm B has no cuts model at all and always passes an empty array. That
 * mapping is deliberately NOT done inside this component — it stays
 * decoupled from src/cuts.ts so Arm B's bundle never needs to pull in Arm
 * A's SOURCE/OUTPUT frame-mapping machinery just to render a filmstrip.
 */
export function VideoAudioLanes({
  durationInFrames,
  pxPerFrame,
  labelWidth,
  rowHeight,
  filmstripUrls,
  waveformUrl,
  spliceFrames,
  spliceTitle,
}: {
  durationInFrames: number;
  pxPerFrame: number;
  labelWidth: number;
  rowHeight: number;
  filmstripUrls?: string[];
  waveformUrl?: string | null;
  spliceFrames?: number[];
  /** Optional per-splice-index tooltip text (Arm A shows "Cut here (N frames removed)"); omit for a bare marker. */
  spliceTitle?: (index: number) => string | undefined;
}) {
  const splices = spliceFrames || [];
  return (
    <>
      <div className="lane" style={{ height: rowHeight * 2 }}>
        <div className="lane__label">
          <span className="lane__swatch" style={{ background: "#6C63FF" }} />
          <span>Video</span>
        </div>
        <div className="lane__track" style={{ left: labelWidth }}>
          <div
            className="clip clip--video"
            style={{ left: 0, width: durationInFrames * pxPerFrame, top: 2, height: rowHeight * 2 - 5 }}
            title="Raw source video"
          >
            {(filmstripUrls || []).map((url, i) => (
              <img key={i} src={url} alt="" draggable={false} />
            ))}
          </div>
          {splices.map((f, i) => (
            <div
              key={i}
              className="splice-marker"
              style={{ left: f * pxPerFrame, top: 2, height: rowHeight * 2 - 5 }}
              title={spliceTitle?.(i)}
            />
          ))}
        </div>
      </div>
      <div className="lane" style={{ height: rowHeight }}>
        <div className="lane__label">
          <span className="lane__swatch" style={{ background: "#4FA8E8" }} />
          <span>Audio</span>
        </div>
        <div className="lane__track" style={{ left: labelWidth }}>
          <div
            className="clip clip--waveform"
            style={{
              left: 0,
              width: durationInFrames * pxPerFrame,
              top: 2,
              height: rowHeight - 5,
              backgroundImage: waveformUrl ? `url(${waveformUrl})` : undefined,
            }}
            title="Audio waveform (linked to the video track's cuts)"
          />
          {splices.map((f, i) => (
            <div
              key={i}
              className="splice-marker"
              style={{ left: f * pxPerFrame, top: 2, height: rowHeight - 5 }}
            />
          ))}
        </div>
      </div>
    </>
  );
}
