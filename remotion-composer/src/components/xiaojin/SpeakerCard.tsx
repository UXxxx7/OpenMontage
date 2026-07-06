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
import { OffthreadVideo, interpolate, staticFile, useCurrentFrame } from "remotion";
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
}

export const SpeakerCard: React.FC<SpeakerCardProps> = ({
  videoSrc,
  scenes,
  opacityKeyframes,
  objectPosition = "50% 35%",
  colorMode = "warm",
  enterFrames = 26,
}) => {
  const frame = useCurrentFrame();
  const palette = PALETTES[colorMode];

  if (scenes.length === 0) return null;

  const opts = {
    extrapolateLeft: "clamp" as const,
    extrapolateRight: "clamp" as const,
    easing: APPLE,
  };

  const frames = scenes.map((s) => s.frame);
  const cx = interpolate(frame, frames, scenes.map((s) => s.x), opts);
  const cy = interpolate(frame, frames, scenes.map((s) => s.y), opts);
  const cw = interpolate(frame, frames, scenes.map((s) => s.w), opts);
  const ch = interpolate(frame, frames, scenes.map((s) => s.h), opts);

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
      <OffthreadVideo
        src={videoSrc.startsWith("http") ? videoSrc : staticFile(videoSrc)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          objectPosition,
          filter: "contrast(1.06) brightness(0.95) saturate(1.06)",
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "linear-gradient(180deg, rgba(20,12,6,0.30) 0%, rgba(20,12,6,0) 32%)",
          pointerEvents: "none",
        }}
      />
    </div>
  );
};
