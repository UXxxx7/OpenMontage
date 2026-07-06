import { useCurrentFrame, useVideoConfig } from "remotion";
import { PROG, RAINBOW, W } from "./theme";

export const RainbowProgressBar: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const pct = Math.min(1, frame / (durationInFrames - 1));
  return (
    <div style={{ position: "absolute", left: 0, top: PROG.y, width: W, height: PROG.h, background: "rgba(0,0,0,0.10)" }}>
      <div style={{ width: W * pct, height: "100%", background: RAINBOW }} />
    </div>
  );
};
