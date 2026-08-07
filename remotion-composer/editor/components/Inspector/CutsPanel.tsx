import React, { useState } from "react";
import { normalizeCuts, outputToSource, totalCutFrames, type VideoCut } from "../../../src/cuts";
import { frameRef, framesToTimecode } from "../../state/playhead";

/**
 * CapCut-style razor cuts, button-first — no drag handles, so this works
 * identically on a phone and a desktop (see Timeline.tsx: the Video/Audio
 * lanes are read-only reference strips now, cuts are made here). Every
 * button reads the Player's own playhead (frameRef, OUTPUT-frame space) at
 * click time and converts it to SOURCE-frame space before writing — cuts
 * themselves are stored/edited as SOURCE-frame regions (src/cuts.ts).
 */
export function CutsPanel({
  cuts,
  sourceDurationFrames,
  onCutsChange,
}: {
  cuts: VideoCut[];
  sourceDurationFrames: number;
  onCutsChange: (next: VideoCut[]) => void;
}) {
  const [markA, setMarkA] = useState<number | null>(null);

  const playSource = () =>
    Math.max(0, Math.min(sourceDurationFrames, outputToSource(frameRef.current, cuts)));

  const addCut = (fromFrame: number, toFrame: number) => {
    if (toFrame <= fromFrame) return;
    onCutsChange(normalizeCuts([...cuts, { fromFrame, toFrame }], sourceDurationFrames));
  };

  const removeCut = (index: number) => {
    onCutsChange(cuts.filter((_, i) => i !== index));
  };

  const finalLength = Math.max(1, sourceDurationFrames - totalCutFrames(cuts));

  return (
    <>
      <div className="nudge">
        <button
          type="button"
          className="btn btn--sm"
          title="Remove everything before the playhead"
          onClick={() => addCut(0, playSource())}
        >
          Cut start here
        </button>
        <button
          type="button"
          className="btn btn--sm"
          title="Remove everything after the playhead"
          onClick={() => addCut(playSource(), sourceDurationFrames)}
        >
          Cut end here
        </button>
      </div>

      <div className="nudge">
        {markA === null ? (
          <button type="button" className="btn btn--sm" onClick={() => setMarkA(playSource())}>
            Mark start of section to cut
          </button>
        ) : (
          <>
            <span className="field__hint">Section starts at {framesToTimecode(markA)}</span>
            <button
              type="button"
              className="btn btn--sm"
              title="Remove everything between the marked start and the current playhead"
              onClick={() => {
                const b = playSource();
                addCut(Math.min(markA, b), Math.max(markA, b));
                setMarkA(null);
              }}
            >
              Cut to playhead
            </button>
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => setMarkA(null)}>
              Cancel
            </button>
          </>
        )}
      </div>

      {cuts.length > 0 && (
        <div style={{ marginTop: 6 }}>
          {cuts.map((c, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, padding: "3px 0" }}>
              <span className="field__hint" style={{ flex: 1 }}>
                {framesToTimecode(c.fromFrame)} → {framesToTimecode(c.toFrame)} ({framesToTimecode(c.toFrame - c.fromFrame)} removed)
              </span>
              <button
                type="button"
                className="btn btn--sm btn--danger"
                title="Undo this cut"
                onClick={() => removeCut(i)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="field__hint" style={{ marginTop: 6 }}>
        Final length: {framesToTimecode(finalLength)} (from {framesToTimecode(sourceDurationFrames)})
      </div>
    </>
  );
}
