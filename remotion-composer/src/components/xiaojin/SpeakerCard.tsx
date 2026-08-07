/**
 * Floating rounded-card speaker treatment — the signature move of the
 * xiaojin-editorial style. Never full-bleed; travels the canvas as a
 * physical card with a drop shadow.
 *
 * Generalized from video-studio's `motion/vell-renewal-reminder/src/VellRenewal/SpeakerCard.tsx`:
 * that version hardcoded one video's exact scene positions/timings. Here the
 * scene schedule is a prop, so this component is reusable across projects.
 *
 * Face-crop calibration is a per-video decision (see video-studio's
 * CLAUDE-v2.md §6a) — `objectPosition` is exposed as a prop for exactly that
 * reason, not hardcoded. There is no default that works across different
 * source videos; the caller must supply a calibrated value.
 */
import { interpolate, useCurrentFrame } from "remotion";
import { keptSegments, normalizeCuts, type VideoCut } from "../../cuts";
import { CutVideo } from "./CutVideo";
import { APPLE, ColorMode, PALETTES } from "./theme";

export interface SpeakerCardScene {
  /** Frame this scene's box takes effect from (interpolated to the next entry). */
  frame: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface SpeakerCardOpacityKeyframe {
  frame: number;
  opacity: number;
}

export interface SpeakerCardProps {
  videoSrc: string;
  scenes: SpeakerCardScene[];
  opacityKeyframes?: SpeakerCardOpacityKeyframe[];
  /**
   * Calibrated per source video: face_center_y_in_source / source_height * 100.
   * Never assume a fixed value works across different source videos.
   */
  objectPosition?: string;
  colorMode?: ColorMode;
  /** Frames for the first-appearance rise-in. Default 26 (matches the reference build). */
  enterFrames?: number;
  /** Editor-authored razor cuts (source-video frames) — see ../../cuts.ts. Absent/empty plays the full, untrimmed source. */
  videoCuts?: VideoCut[];
  /** Full untrimmed source length in frames — required to derive kept segments from videoCuts. */
  sourceDurationFrames: number;
  /** Passed straight to CutVideo/OffthreadVideo. Absent/undefined = full volume (unchanged from every job before this field existed). */
  videoVolume?: number;
  /**
   * Rendered inside the card's own positioned div (e.g. CornerCard) — so an
   * overlay anchored here travels/scales/hides with the card automatically
   * instead of needing to duplicate this component's scene interpolation.
   */
  children?: React.ReactNode;
}

// A bare multi-point interpolate() over sparse, widely-spaced scenes glides
// continuously across the ENTIRE gap between two keyframes (confirmed via
// render test: a 400-frame gap produced a card still visibly resizing/
// moving at every checked frame in between) — not the quick-transition-
// then-hold behavior compose-director.md documents ("spring translate X,
// not a cut") and video-studio's own SpeakerCard implementations already
// give. Fix: insert a hold keyframe TRANSITION_FRAMES before every real
// scene change, so interpolate() holds flat at the previous value and only
// actually moves in a fixed window right before each entry. Already-dense/
// duplicate-authored scenes (e.g. two identical entries marking a static
// hold) pass through unaffected — the inserted hold point just duplicates
// values that were already equal.
const TRANSITION_FRAMES = 20;

function withHoldKeyframes(scenes: SpeakerCardScene[]): SpeakerCardScene[] {
  if (scenes.length <= 1) return scenes;
  const expanded: SpeakerCardScene[] = [scenes[0]];
  for (let i = 1; i < scenes.length; i++) {
    const prev = scenes[i - 1];
    const next = scenes[i];
    const holdFrame = next.frame - TRANSITION_FRAMES;
    if (holdFrame > prev.frame) {
      expanded.push({ frame: holdFrame, x: prev.x, y: prev.y, w: prev.w, h: prev.h });
    }
    expanded.push(next);
  }
  return expanded;
}

export interface SpeakerRect { x: number; y: number; w: number; h: number }

/**
 * Pure: the facecam's on-screen rect at a given frame, including the
 * synthetic hold keyframes withHoldKeyframes() inserts — this IS the exact
 * math the component below renders with (extracted, not reimplemented), so
 * a caller gets a byte-exact rect rather than an approximation. Exists for
 * the editor's preview click/select overlay (state/staticElements.ts) to
 * make the facecam and anything anchored to it (CornerCard) selectable
 * without duplicating this interpolation.
 *
 * Returns null when there are no scenes — nothing to compute, matches the
 * component's own `if (scenes.length === 0) return null`.
 */
export function speakerRectAt(scenes: SpeakerCardScene[], frame: number): SpeakerRect | null {
  if (scenes.length === 0) return null;
  const opts = {
    extrapolateLeft: "clamp" as const,
    extrapolateRight: "clamp" as const,
    easing: APPLE,
  };
  const holdScenes = withHoldKeyframes(scenes);
  const frames = holdScenes.map((s) => s.frame);
  return {
    x: interpolate(frame, frames, holdScenes.map((s) => s.x), opts),
    y: interpolate(frame, frames, holdScenes.map((s) => s.y), opts),
    w: interpolate(frame, frames, holdScenes.map((s) => s.w), opts),
    h: interpolate(frame, frames, holdScenes.map((s) => s.h), opts),
  };
}

export const SpeakerCard: React.FC<SpeakerCardProps> = ({
  videoSrc,
  scenes,
  opacityKeyframes,
  objectPosition = "50% 35%",
  colorMode = "warm",
  enterFrames = 26,
  videoCuts,
  sourceDurationFrames,
  videoVolume,
  children,
}) => {
  const frame = useCurrentFrame();
  const palette = PALETTES[colorMode];
  // Never trust that props.videoCuts arrived already sorted/merged/clamped
  // — normalize here too, at the point of use, same "guarantee it where the
  // value is actually consumed" discipline this codebase already applies
  // elsewhere (see whatsapp_mvp/CLAUDE.md's Rule 22).
  const cuts = normalizeCuts(videoCuts, sourceDurationFrames);
  const segments = keptSegments(cuts, sourceDurationFrames);

  const rect = speakerRectAt(scenes, frame);
  if (!rect) return null;
  const { x: cx, y: cy, w: cw, h: ch } = rect;

  const opts = {
    extrapolateLeft: "clamp" as const,
    extrapolateRight: "clamp" as const,
    easing: APPLE,
  };

  const cardOpacity = opacityKeyframes && opacityKeyframes.length > 0
    ? interpolate(
        frame,
        opacityKeyframes.map((k) => k.frame),
        opacityKeyframes.map((k) => k.opacity),
        opts
      )
    : 1;

  const enter = interpolate(frame, [0, enterFrames], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: APPLE,
  });

  return (
    <div
      style={{
        position: "absolute",
        left: cx,
        top: cy,
        width: cw,
        height: ch,
        opacity: enter * cardOpacity,
        transform: `translateY(${(1 - enter) * 90}px)`,
        borderRadius: 28,
        overflow: "hidden",
        boxShadow: [
          `0 36px 90px ${palette.shadow}`,
          `0 12px 32px rgba(0,0,0,0.20)`,
          `inset 0 1.5px 0 rgba(255,255,255,0.30)`,
        ].join(", "),
        border: `5px solid ${palette.card}`,
        background: "#000",
      }}
    >
      <CutVideo
        src={videoSrc}
        segments={segments}
        sourceDurationFrames={sourceDurationFrames}
        volume={videoVolume}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          objectPosition,
          filter: "contrast(1.06) brightness(0.88) saturate(1.06)",
        }}
      />
      {/* Top scrim to tame blown-out exposure/lens flare — the 0.30-opacity/
          32%-falloff version this replaced was too weak/shallow for footage
          with a genuinely overexposed light source (confirmed against real
          production footage: highlights stayed blown out past 32% down). */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "linear-gradient(180deg, rgba(20,12,6,0.55) 0%, rgba(20,12,6,0) 45%)",
          pointerEvents: "none",
        }}
      />
      {children}
    </div>
  );
};
