/**
 * Presenter — a small rounded speaker inset shown in the LOWER zone (below the
 * floating SpeakerCard) during b-roll windows.
 *
 * Why this exists: when a job has both insert_broll and apply_style, the
 * pipeline runs insert_broll in "presenter mode" — the card (SpeakerCard's
 * videoSrc) shows the b-roll full-bleed via cutaway, with NO speaker baked in,
 * so nothing covers the b-roll. The speaker is instead rendered here, as a
 * separate video layer positioned in the empty lower zone. The speaker video
 * shares the composition's exact timeline (it is the pre-insert_broll edited
 * source), so an always-mounted <OffthreadVideo> stays in sync; we only fade
 * it in during the b-roll windows and hide it otherwise (outside a window the
 * card already shows the speaker, so a second copy would be redundant).
 *
 * Muted on purpose: audio comes from the card's videoSrc (cutaway keeps the
 * speaker's original voice). A second audible copy would double the voice.
 *
 * Position/size come entirely from props (x/y/w/h) so the layout can be tuned
 * from the pipeline side without recompiling the bundle.
 */
import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { keptSegments, normalizeCuts, type VideoCut } from "../../cuts";
import { CutVideo } from "./CutVideo";

export interface PresenterWindow {
  fromFrame: number;
  toFrame: number;
}

export interface PresenterProps {
  src: string;
  windows: PresenterWindow[];
  x: number;
  y: number;
  w: number;
  h: number;
  radius?: number;
  objectPosition?: string;
  colorMode?: "warm" | "dark";
  /** Same cuts as the main SpeakerCard video — this source shares the composition's original (pre-cut) timeline, per this file's own doc comment. */
  videoCuts?: VideoCut[];
  sourceDurationFrames: number;
}

const FADE = 8; // frames of fade in/out around each window

export const Presenter: React.FC<PresenterProps> = ({
  src,
  windows,
  x,
  y,
  w,
  h,
  radius = 28,
  objectPosition = "50% 50%",
  colorMode = "warm",
  videoCuts,
  sourceDurationFrames,
}) => {
  const frame = useCurrentFrame();
  // `windows` arrives already mapped to OUTPUT frames (XiaojinEditorial.tsx
  // maps the whole `presenter` object via mapPropsForCuts before spreading
  // it here) — `frame` from useCurrentFrame() is output-space too, so the
  // fade/opacity math below is untouched. The VIDEO ITSELF needs the
  // opposite: trimBefore/trimAfter are SOURCE-frame values, so segments are
  // derived from the raw (source-space) cuts, not the mapped windows.
  const cuts = normalizeCuts(videoCuts, sourceDurationFrames);
  const segments = keptSegments(cuts, sourceDurationFrames);

  // Visible ONLY strictly inside a b-roll window, with the fade kept INSIDE the
  // window (fade in over the first FADE frames, out over the last FADE frames).
  // Critical: the card only shows b-roll between fromFrame..toFrame (ffmpeg hard
  // cut); if this inset extended even one frame beyond that, the card would still
  // be showing the speaker there and the face would appear in BOTH places. So
  // opacity is 0 at both window edges and never leaks outside [fromFrame,toFrame].
  let opacity = 0;
  for (const win of windows || []) {
    if (frame >= win.fromFrame && frame <= win.toFrame) {
      const rampIn = (frame - win.fromFrame) / FADE;
      const rampOut = (win.toFrame - frame) / FADE;
      const o = Math.max(0, Math.min(1, rampIn, rampOut));
      if (o > opacity) opacity = o;
    }
  }
  if (opacity <= 0) return null;

  const border = colorMode === "warm" ? "#FFFFFF" : "#1B2129";

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div
        style={{
          position: "absolute",
          left: x,
          top: y,
          width: w,
          height: h,
          opacity,
          borderRadius: radius,
          overflow: "hidden",
          border: `6px solid ${border}`,
          boxShadow: "0 12px 40px rgba(0,0,0,0.28)",
          background: "#000",
        }}
      >
        <CutVideo
          src={src}
          segments={segments}
          sourceDurationFrames={sourceDurationFrames}
          muted
          style={{
            width: "100%",
            height: "100%",
            objectFit: "cover",
            objectPosition,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};
