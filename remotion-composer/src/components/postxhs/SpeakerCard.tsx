/**
 * Floating speaker card — post-xhs style. Never full-bleed. Two modes,
 * both ratio 0.565 (see theme.ts): Dominant (default, most of the runtime)
 * and Workflow (corner pip, only when a section needs full-canvas graphic
 * space). Ported from compose-director.md's SpeakerCard spec.
 *
 * Ratio/crop policy: sourceRatio within ±12% of 0.565 (typical vertical
 * selfie/webcam) uses cover+objectPosition; outside that (e.g. landscape
 * source) uses contain, with letterbox bars filled by a blurred+darkened
 * copy of the same footage instead of a plain black bar.
 */
import { AbsoluteFill, OffthreadVideo, interpolate, staticFile, useCurrentFrame } from "remotion";
import { APPLE, CARD_RATIO, COLOR, DOMINANT, WORKFLOW } from "./theme";

export type SpeakerMode = "dominant" | "workflow";

export interface SpeakerModeEntry {
  frame: number;
  mode: SpeakerMode;
}

export interface SpeakerCardProps {
  videoSrc: string;
  sourceWidth: number;
  sourceHeight: number;
  /**
   * Calibrated per source video per mode actually used:
   * objPos ~= (face_center_y_in_source / source_height) * 100.
   * Never assume a fixed value works across different source videos
   * (compose-director.md's objectPosition Calibration procedure).
   */
  dominantObjPos: number;
  workflowObjPos?: number;
  /** Ordered mode schedule. Defaults to Dominant for the whole duration. */
  modeSchedule?: SpeakerModeEntry[];
  /** Frame the outro starts — card fades 1.0 -> 0.0 over 20 frames from here. */
  outroFromFrame?: number;
}

const boxFor = (mode: SpeakerMode) => (mode === "dominant" ? DOMINANT : WORKFLOW);

// compose-director.md: "Transition: APPLE easing over 20 frames." A mode
// schedule entry means "the box SNAPS to this mode's box by this frame" —
// it does not mean "interpolate continuously from the previous entry's
// frame all the way to this one." Feeding modeSchedule's frames straight
// into interpolate() as keyframes (the original, buggy version of this
// component) made the card drift continuously for the entire gap between
// two schedule entries — e.g. slowly shrinking for 16 real seconds instead
// of holding still and snapping in the last 20 frames. This builds actual
// hold-then-transition keyframes so the box is stable except in a short
// window right before each scheduled switch.
const TRANSITION_FRAMES = 20;

function buildKeyframes<T>(
  modeSchedule: SpeakerModeEntry[],
  valueFor: (mode: SpeakerMode) => T
): { frames: number[]; values: T[] } {
  const frames: number[] = [];
  const values: T[] = [];
  modeSchedule.forEach((entry, i) => {
    if (i === 0) {
      frames.push(entry.frame);
      values.push(valueFor(entry.mode));
      return;
    }
    const prevFrame = modeSchedule[i - 1].frame;
    const holdFrame = Math.min(entry.frame - 1, Math.max(prevFrame, entry.frame - TRANSITION_FRAMES));
    if (holdFrame > frames[frames.length - 1]) {
      frames.push(holdFrame);
      values.push(valueFor(modeSchedule[i - 1].mode));
    }
    frames.push(entry.frame);
    values.push(valueFor(entry.mode));
  });
  return { frames, values };
}

export const SpeakerCard: React.FC<SpeakerCardProps> = ({
  videoSrc,
  sourceWidth,
  sourceHeight,
  dominantObjPos,
  workflowObjPos,
  modeSchedule = [{ frame: 0, mode: "dominant" }],
  outroFromFrame,
}) => {
  const frame = useCurrentFrame();
  const opts = { extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const, easing: APPLE };

  const xKf = buildKeyframes(modeSchedule, (m) => boxFor(m).x);
  const yKf = buildKeyframes(modeSchedule, (m) => boxFor(m).y);
  const wKf = buildKeyframes(modeSchedule, (m) => boxFor(m).w);
  const hKf = buildKeyframes(modeSchedule, (m) => boxFor(m).h);
  const x = interpolate(frame, xKf.frames, xKf.values, opts);
  const y = interpolate(frame, yKf.frames, yKf.values, opts);
  const w = interpolate(frame, wKf.frames, wKf.values, opts);
  const h = interpolate(frame, hKf.frames, hKf.values, opts);

  // objPos transitions on the same hold-then-snap schedule as the box.
  const objPosKf = buildKeyframes(modeSchedule, (m) =>
    m === "dominant" ? dominantObjPos : workflowObjPos ?? dominantObjPos
  );
  const objPos = interpolate(frame, objPosKf.frames, objPosKf.values, opts);

  const outroOpacity = outroFromFrame
    ? interpolate(frame, [outroFromFrame, outroFromFrame + 20], [1, 0], opts)
    : 1;

  const sourceRatio = sourceWidth / sourceHeight;
  const ratioDelta = Math.abs(sourceRatio - CARD_RATIO) / CARD_RATIO;
  const useCover = ratioDelta <= 0.12;

  const resolvedSrc = videoSrc.startsWith("http") ? videoSrc : staticFile(videoSrc);

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width: w,
        height: h,
        opacity: outroOpacity,
        borderRadius: 24,
        overflow: "hidden",
        border: "5px solid #FFFFFF",
        boxShadow: [
          `0 36px 90px ${COLOR.shadow}`,
          `0 12px 32px rgba(0,0,0,0.20)`,
          `inset 0 1.5px 0 rgba(255,255,255,0.52)`,
        ].join(", "),
        background: "#000",
      }}
    >
      {useCover ? (
        <OffthreadVideo
          src={resolvedSrc}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "cover",
            objectPosition: `50% ${objPos}%`,
            filter: "contrast(1.05) brightness(1.02) saturate(1.04)",
          }}
        />
      ) : (
        <AbsoluteFill>
          <OffthreadVideo
            src={resolvedSrc}
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              objectFit: "cover",
              filter: "blur(40px) brightness(0.5)",
            }}
          />
          <OffthreadVideo
            src={resolvedSrc}
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              objectFit: "contain",
              filter: "contrast(1.05) brightness(1.02) saturate(1.04)",
            }}
          />
        </AbsoluteFill>
      )}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: "linear-gradient(180deg, rgba(20,12,6,0.22) 0%, rgba(20,12,6,0) 32%)",
          pointerEvents: "none",
        }}
      />
    </div>
  );
};
