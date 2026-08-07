import React, { useState } from "react";
import { Player, Thumbnail } from "@remotion/player";
import { CardPreview } from "../../../src/CardPreview";
import { humanLabel } from "../../fieldHints";

const PREVIEW_DURATION_FRAMES = 250;
const REST_FRAME = 45; // ~1.5s in — past the entrance spring, before anything starts exiting

/**
 * One gallery tile: a still <Thumbnail> at rest, swapped for a real,
 * looping <Player> on hover/tap — never more than one live Player mounted
 * across the whole panel at a time, which is what keeps ~23 tiles cheap.
 *
 * Desktop: hover previews, clicking anywhere adds the card at the playhead
 * (unchanged — this panel is a visual front end for the same action
 * AddMenu's entries use, not a separate code path).
 *
 * Touch (Phase 6, F3c): `pointerenter` fires on tap-DOWN and `onClick` on
 * tap-UP, so the desktop behavior above collapses into "every tap adds
 * immediately" — there is no way to preview a card without committing it.
 * Fixed by splitting into two controls: tapping the preview toggles it
 * inline (mirroring hover), and a separate "+ Add" button — a SIBLING, not
 * nested inside the preview control, since a <button> can't validly
 * contain another <button> — commits. onAdd still closes the loop exactly
 * once either way.
 */
export function LibraryTile({
  section,
  item,
  onAdd,
  isTouch,
}: {
  section: string;
  item: Record<string, unknown>;
  onAdd: (section: string) => void;
  isTouch: boolean;
}) {
  const [active, setActive] = useState(false);

  const inputProps = { section, item };

  return (
    <div className={`library-tile${active ? " library-tile--active" : ""}`}>
      <button
        type="button"
        className="library-tile__preview-btn"
        onClick={() => (isTouch ? setActive((a) => !a) : onAdd(section))}
        onPointerEnter={() => { if (!isTouch) setActive(true); }}
        onPointerLeave={() => { if (!isTouch) setActive(false); }}
        title={isTouch ? `Preview ${humanLabel(section)}` : `Add ${humanLabel(section)}`}
      >
        <div className="library-tile__preview">
          {active ? (
            <Player
              component={CardPreview}
              inputProps={inputProps}
              durationInFrames={PREVIEW_DURATION_FRAMES}
              fps={30}
              compositionWidth={1080}
              compositionHeight={1920}
              style={{ width: "100%", height: "100%" }}
              acknowledgeRemotionLicense
              autoPlay
              loop
              controls={false}
              clickToPlay={false}
              spaceKeyToPlayOrPause={false}
            />
          ) : (
            <Thumbnail
              component={CardPreview}
              inputProps={inputProps}
              frameToDisplay={REST_FRAME}
              durationInFrames={PREVIEW_DURATION_FRAMES}
              fps={30}
              compositionWidth={1080}
              compositionHeight={1920}
              style={{ width: "100%", height: "100%" }}
            />
          )}
        </div>
        <span className="library-tile__label">{humanLabel(section)}</span>
      </button>
      {isTouch && active && (
        <button type="button" className="library-tile__add" onClick={() => onAdd(section)}>
          + Add
        </button>
      )}
    </div>
  );
}
