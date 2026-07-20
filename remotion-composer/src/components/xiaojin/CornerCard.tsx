/**
 * CornerCard — compact illustration overlay rendered INSIDE the SpeakerCard
 * (passed as its `children`, see SpeakerCard.tsx), bottom-left corner, so it
 * travels/scales with the card automatically and never needs its own
 * geometry math. Generalizes video-studio's dajaai-walking-fresh
 * WorkflowVisuals.tsx (WhatsApp bubble / AI progress ring) — two starter
 * variants, "chat" and "progress".
 *
 * Never rendered during a section takeover or quote (the SpeakerCard is
 * hidden then, so a child overlay would be invisible/wasted anyway) —
 * enforced planner-side (content_planner.py), not here.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export interface CornerCardChatProps {
  variant: "chat";
  appName: string;
  message: string;
  mountFrame: number;
  endFrame?: number;
}

export interface CornerCardProgressProps {
  variant: "progress";
  label: string;
  percent: number;
  mountFrame: number;
  endFrame?: number;
}

export type CornerCardProps = CornerCardChatProps | CornerCardProgressProps;

const W = 380;

const ProgressRing: React.FC<{ percent: number }> = ({ percent }) => {
  const R = 26;
  const CIRC = 2 * Math.PI * R;
  return (
    <svg width={64} height={64} style={{ flexShrink: 0 }}>
      <circle cx={32} cy={32} r={R} fill="none" stroke="rgba(0,0,0,0.1)" strokeWidth={6} />
      <circle
        cx={32} cy={32} r={R} fill="none" stroke="#4D9EFF" strokeWidth={6}
        strokeLinecap="round" strokeDasharray={CIRC}
        strokeDashoffset={CIRC * (1 - percent / 100)}
        transform="rotate(-90 32 32)"
      />
      <text x={32} y={38} textAnchor="middle" fontSize={16} fontWeight={800} fill="#222">
        {Math.round(percent)}%
      </text>
    </svg>
  );
};

export const CornerCard: React.FC<CornerCardProps> = (props) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { mountFrame, endFrame } = props;
  const local = frame - mountFrame;
  if (local < 0) return null;
  if (endFrame !== undefined && frame >= endFrame) return null;
  const exitFade = endFrame !== undefined
    ? interpolate(frame, [endFrame - 15, endFrame], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  const pop = spring({ frame: local, fps, config: { damping: 15, stiffness: 210 } });

  return (
    <div
      style={{
        position: "absolute", left: 20, bottom: 20, width: W,
        background: "rgba(255,255,255,0.96)",
        borderRadius: 18,
        boxShadow: "0 10px 28px rgba(0,0,0,0.28)",
        opacity: pop * exitFade,
        transform: `scale(${0.85 + 0.15 * pop})`,
        transformOrigin: "bottom left",
        overflow: "hidden",
      }}
    >
      {props.variant === "chat" ? (
        <>
          <div style={{ background: "#25D366", padding: "10px 16px", fontSize: 14, fontWeight: 700, color: "#fff" }}>
            {props.appName}
          </div>
          <div style={{ padding: "14px 16px" }}>
            <div style={{
              background: "#DCF8C6", borderRadius: "12px 12px 12px 2px",
              padding: "10px 14px", fontSize: 16, fontWeight: 500, color: "#111",
              display: "inline-block", maxWidth: "100%",
            }}>
              {props.message}
            </div>
          </div>
        </>
      ) : (
        <div style={{ padding: "18px 20px", display: "flex", alignItems: "center", gap: 16 }}>
          <ProgressRing percent={interpolate(local, [0, 40], [0, props.percent], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })} />
          <span style={{ fontSize: 16, fontWeight: 700, color: "#222" }}>{props.label}</span>
        </div>
      )}
    </div>
  );
};
