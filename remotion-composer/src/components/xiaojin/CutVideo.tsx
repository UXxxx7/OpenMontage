/**
 * Renders a source video with zero or more "razor" regions cut out (see
 * ../../cuts.ts) — the shared segment renderer both SpeakerCard and
 * Presenter use, since both play the same kind of cut source video on the
 * same output timeline.
 *
 * With no cuts (the overwhelmingly common case — every job before this
 * feature existed, and every job the user hasn't cut yet) this renders
 * EXACTLY one bare <OffthreadVideo>, byte-identical to what SpeakerCard
 * rendered before cuts existed — no <Sequence> wrapper, no behavior change.
 *
 * With cuts: one <Sequence from={segment.outFrom} durationInFrames={...}>
 * per KEPT segment, each containing an <OffthreadVideo trimBefore/trimAfter>
 * for that segment's SOURCE range. Verified against Remotion 4.0.484's own
 * source (Sequence.js / OffthreadVideo.js / validate-start-from-props.js):
 * trimBefore/trimAfter are absolute media-frame indices and compose
 * correctly with an enclosing Sequence's `from` offset — source frames
 * [trimBefore, trimAfter) play across output frames
 * [from, from + (trimAfter - trimBefore)).
 *
 * `premountFor` matters for the live Player specifically (it's a no-op
 * during `remotion render` — Sequence.js only honors it when
 * `!env.isRendering`): without it, each segment's underlying <video> mounts
 * cold at its own splice point and `use-media-buffering.js` calls
 * `delayPlayback()`, stalling the WHOLE Player timeline at every cut.
 * Premounting parks the element at the right source time ahead of the
 * playhead arriving, so it's already decoded and ready.
 */
import { OffthreadVideo, Sequence, staticFile } from "remotion";

export interface CutVideoSegment {
  /** Start of this kept segment, in SOURCE video frames. */
  srcFrom: number;
  /** End of this kept segment, in SOURCE video frames (exclusive). */
  srcTo: number;
  /** Where this segment starts in the OUTPUT composition's own frame numbering. */
  outFrom: number;
}

export interface CutVideoProps {
  src: string;
  /** Kept segments from src/cuts.ts's keptSegments() — already in OUTPUT-frame order. */
  segments: CutVideoSegment[];
  /** Full untrimmed source length in frames — used only to detect the true no-cuts case. */
  sourceDurationFrames: number;
  style?: React.CSSProperties;
  /** Frames to premount each non-first segment ahead of the playhead, live-Player only. */
  premountFor?: number;
  /** Passed straight to every underlying OffthreadVideo — Presenter's inset plays muted (audio comes from the main card). */
  muted?: boolean;
  /** Passed straight to every underlying OffthreadVideo. Absent/undefined = full volume (Remotion's own OffthreadVideo default) — every job before this field existed is unaffected. */
  volume?: number;
}

export const CutVideo: React.FC<CutVideoProps> = ({
  src,
  segments,
  sourceDurationFrames,
  style,
  premountFor = 30,
  muted,
  volume,
}) => {
  const resolvedSrc = src.startsWith("http") ? src : staticFile(src);

  const isNoOp =
    segments.length === 0 ||
    (segments.length === 1 && segments[0].srcFrom === 0 && segments[0].srcTo === sourceDurationFrames);

  if (isNoOp) {
    return <OffthreadVideo src={resolvedSrc} style={style} muted={muted} volume={volume} />;
  }

  return (
    <>
      {segments.map((s, i) => (
        <Sequence
          key={`${s.srcFrom}-${s.srcTo}`}
          from={s.outFrom}
          durationInFrames={Math.max(1, s.srcTo - s.srcFrom)}
          premountFor={i === 0 ? 0 : premountFor}
          showInTimeline={false}
          style={{ display: "block" }}
        >
          <OffthreadVideo
            src={resolvedSrc}
            trimBefore={s.srcFrom}
            trimAfter={s.srcTo}
            style={style}
            muted={muted}
            volume={volume}
          />
        </Sequence>
      ))}
    </>
  );
};
