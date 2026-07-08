import {
  AbsoluteFill,
  Audio,
  CalculateMetadataFunction,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { getVideoMetadata } from "@remotion/media-utils";

type CaptionCue = {
  start: number;
  end: number;
  text: string;
};

type InfoCue = {
  start: number;
  end: number;
  label: string;
  x: number;
  y: number;
  width?: number;
};

type LayoutCue = {
  start: number;
  end: number;
  mode: "full" | "left" | "right";
};

export interface ReferenceStyleEditProps extends Record<string, unknown> {
  videoSrc: string;
  videoFit?: "cover" | "contain";
  durationSeconds?: number;
  sourceStartSeconds?: number;
  captions: CaptionCue[];
  infoCues: InfoCue[];
  layoutCues: LayoutCue[];
  navItems: string[];
}

const resolveAsset = (src: string): string => {
  if (
    src.startsWith("http://") ||
    src.startsWith("https://") ||
    src.startsWith("data:")
  ) {
    return src;
  }
  const clean = src.replace(/^file:\/\/\/?/, "");
  if (clean.startsWith("/") || /^[A-Za-z]:[\\/]/.test(clean)) {
    return `file:///${clean.replace(/\\/g, "/")}`;
  }
  return staticFile(clean);
};

const pickCue = <T extends { start: number; end: number }>(
  cues: T[],
  seconds: number
): T | undefined => cues.find((cue) => seconds >= cue.start && seconds < cue.end);

const TopNav: React.FC<{ items: string[] }> = ({ items }) => (
  <div
    style={{
      position: "absolute",
      top: 0,
      left: 0,
      right: 0,
      height: 36,
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "0 32px",
      background: "rgba(42, 38, 31, 0.72)",
      color: "rgba(255,255,255,0.92)",
      fontFamily:
        "'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', Arial, sans-serif",
      fontSize: 13,
      fontWeight: 700,
      whiteSpace: "nowrap",
      boxSizing: "border-box",
    }}
  >
    {items.map((item, index) => (
      <span key={`${item}-${index}`}>
        {item}
        {index < items.length - 1 ? (
          <span style={{ marginLeft: 28, color: "rgba(255,255,255,0.78)" }}>|</span>
        ) : null}
      </span>
    ))}
  </div>
);

const SoftBackdrop: React.FC = () => {
  const frame = useCurrentFrame();
  const x = 50 + Math.sin(frame / 110) * 8;
  const y = 46 + Math.cos(frame / 140) * 6;

  return (
    <AbsoluteFill
      style={{
        background:
          "linear-gradient(135deg, #f2e3cc 0%, #f8efd9 38%, #eee0c8 100%)",
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 0,
          backgroundImage:
            "linear-gradient(90deg, rgba(255,255,255,0.3) 1px, transparent 1px), linear-gradient(rgba(255,255,255,0.28) 1px, transparent 1px)",
          backgroundSize: "56px 56px",
          opacity: 0.18,
        }}
      />
      <div
        style={{
          position: "absolute",
          left: `${x}%`,
          top: `${y}%`,
          width: 760,
          height: 760,
          borderRadius: "50%",
          transform: "translate(-50%, -50%)",
          background:
            "radial-gradient(circle, rgba(255,255,255,0.52) 0%, rgba(255,255,255,0.2) 38%, rgba(255,255,255,0) 68%)",
        }}
      />
    </AbsoluteFill>
  );
};

const VideoCard: React.FC<{
  src: string;
  mode: LayoutCue["mode"];
  sourceStartSeconds: number;
  fit?: "cover" | "contain";
}> = ({ src, mode, sourceStartSeconds, fit = "cover" }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const entry = spring({
    frame,
    fps,
    config: { damping: 20, stiffness: 95, mass: 0.9 },
  });

  const styleByMode = {
    full: { left: 252, top: 74, width: 676, height: 944 },
    left: { left: 52, top: 80, width: 536, height: 944 },
    right: { left: 858, top: 102, width: 370, height: 716 },
  }[mode];

  return (
    <div
      style={{
        position: "absolute",
        ...styleByMode,
        borderRadius: 18,
        overflow: "hidden",
        background: "#111",
        border: "1px solid rgba(255,255,255,0.86)",
        boxShadow: "0 28px 48px rgba(43,35,24,0.34)",
        transform: `translateY(${interpolate(entry, [0, 1], [22, 0])}px) scale(${interpolate(entry, [0, 1], [0.985, 1])})`,
      }}
    >
      <OffthreadVideo
        src={resolveAsset(src)}
        startFrom={Math.round(sourceStartSeconds * fps)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: fit,
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "linear-gradient(180deg, rgba(0,0,0,0.06), rgba(0,0,0,0.2))",
          pointerEvents: "none",
        }}
      />
    </div>
  );
};

const InfoPill: React.FC<{ cue: InfoCue }> = ({ cue }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const localFrame = frame - Math.round(cue.start * fps);
  const enter = spring({
    frame: localFrame,
    fps,
    config: { damping: 18, stiffness: 120 },
  });

  return (
    <div
      style={{
        position: "absolute",
        left: cue.x,
        top: cue.y,
        width: cue.width ?? 420,
        minHeight: 54,
        borderRadius: 9,
        padding: "0 24px",
        display: "flex",
        alignItems: "center",
        gap: 16,
        color: "rgba(255,255,255,0.94)",
        fontFamily:
          "'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', Arial, sans-serif",
        fontSize: 24,
        fontWeight: 800,
        background: "rgba(66, 63, 58, 0.72)",
        border: "1px solid rgba(255,255,255,0.18)",
        boxShadow: "0 18px 32px rgba(0,0,0,0.18)",
        backdropFilter: "blur(14px)",
        opacity: enter,
        transform: `translateY(${interpolate(enter, [0, 1], [18, 0])}px)`,
      }}
    >
      <span style={{ color: "#f4f0e7", fontSize: 24 }}>✦</span>
      <span>{cue.label}</span>
    </div>
  );
};

const FloatingPanel: React.FC = () => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [270, 330, 690, 750], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <div
      style={{
        position: "absolute",
        left: 646,
        top: 292,
        width: 548,
        height: 160,
        opacity,
      }}
    >
      {["锁定主体情绪", "保留真实现场", "强化短视频节奏"].map((line, index) => (
        <div
          key={line}
          style={{
            height: 42,
            marginBottom: 12,
            borderRadius: 8,
            display: "flex",
            alignItems: "center",
            padding: "0 22px",
            gap: 14,
            color: "rgba(255,255,255,0.88)",
            background: "rgba(74,70,64,0.58)",
            fontSize: 19,
            fontWeight: 800,
            fontFamily:
              "'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', Arial, sans-serif",
            transform: `translateX(${interpolate(
              frame,
              [300 + index * 8, 338 + index * 8],
              [26, 0],
              { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
            )}px)`,
          }}
        >
          <span>✦</span>
          <span>{line}</span>
        </div>
      ))}
    </div>
  );
};

const CaptionBar: React.FC<{ text: string; mode: LayoutCue["mode"] }> = ({
  text,
  mode,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const pop = spring({
    frame,
    fps,
    config: { damping: 18, stiffness: 110 },
  });
  const inSideCanvas = mode === "left" || mode === "right";

  return (
    <div
      style={{
        position: "absolute",
        left: inSideCanvas ? 674 : "50%",
        bottom: inSideCanvas ? 58 : 28,
        transform: inSideCanvas
          ? `scale(${interpolate(pop, [0, 1], [0.98, 1])})`
          : `translateX(-50%) scale(${interpolate(pop, [0, 1], [0.98, 1])})`,
        width: inSideCanvas ? 460 : undefined,
        maxWidth: inSideCanvas ? 460 : 760,
        padding: "6px 14px 9px",
        borderRadius: 7,
        background: "rgba(54, 52, 48, 0.7)",
        color: "#fff",
        fontFamily:
          "'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', Arial, sans-serif",
        fontSize: inSideCanvas ? 24 : 28,
        lineHeight: 1.22,
        fontWeight: 900,
        textAlign: "center",
        textShadow: "0 2px 4px rgba(0,0,0,0.28)",
        letterSpacing: 0,
        whiteSpace: "pre-wrap",
      }}
    >
      {text}
    </div>
  );
};

export const ReferenceStyleEdit: React.FC<ReferenceStyleEditProps> = ({
  videoSrc,
  videoFit = "cover",
  durationSeconds = 30,
  sourceStartSeconds = 0,
  captions,
  infoCues,
  layoutCues,
  navItems,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const seconds = frame / fps;
  const activeCaption = pickCue(captions, seconds);
  const activeInfo = pickCue(infoCues, seconds);
  const activeLayout = pickCue(layoutCues, seconds) ?? {
    start: 0,
    end: durationSeconds,
    mode: "full" as const,
  };

  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      <SoftBackdrop />
      {navItems.length > 0 ? <TopNav items={navItems} /> : null}
      <VideoCard
        src={videoSrc}
        mode={activeLayout.mode}
        sourceStartSeconds={sourceStartSeconds}
        fit={videoFit}
      />
      {activeInfo ? <InfoPill cue={activeInfo} /> : null}
      {activeCaption ? (
        <CaptionBar text={activeCaption.text} mode={activeLayout.mode} />
      ) : null}
    </AbsoluteFill>
  );
};

export const calculateReferenceStyleEditMetadata: CalculateMetadataFunction<
  ReferenceStyleEditProps
> = async ({ props }) => {
  const fps = 30;
  let durationSeconds = props.durationSeconds ?? 30;

  if (!props.durationSeconds) {
    try {
      const meta = await getVideoMetadata(resolveAsset(props.videoSrc));
      durationSeconds = Math.min(30, meta.durationInSeconds);
    } catch {
      durationSeconds = 30;
    }
  }

  return {
    durationInFrames: Math.max(1, Math.round(durationSeconds * fps)),
    fps,
    width: 1280,
    height: 720,
  };
};
