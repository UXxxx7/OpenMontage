/**
 * TimelineSection -- bespoke, full-canvas multi-stage process timeline.
 * Ported from the OpenMontage-agentic-experiment sandbox build
 * (MrBeastAgentic/TimelineSection.tsx, originally "5-stage idea to upload
 * process") that Marvelle reviewed and approved, generalized here so any
 * N-stage process can use it (heading is now a prop instead of hardcoded).
 *
 * A vertical track with a faint always-visible base line and an
 * accent-colored fill that grows node-by-node as each stage is spoken,
 * arriving at each dot exactly on that stage's beat frame. Each node's
 * duration counts up rather than appearing as static text.
 *
 * Fix D6（2026-07-16）：颜色曾经完全写死为深色画布配色，SectionLayer 因此
 * 只在 mode==='dark' 时才渲染这个图形，content_planner 又因此强制把每个
 * process_timeline 章节的 dark 标成 true——三层叠在一起的结果是：默认
 * colorMode（"warm"，大多数任务的实际配色）永远用不上这个组件，真实
 * backtest 截图证实了后果：一个"3 步流程"的 takeover 章节只剩标题+一个
 * 装饰性光斑+底下够不着的 topic cards，大片空白（用户原话："还有这么多
 * 空白，还不如直接把说话人留在那，别为了一个叫 process 的标题就撤了"）。
 * 现在改成读 theme.ts 的 PALETTES，跟其它组件一样按 colorMode 取色——
 * warm 模式下这个图形完全能用，不用再靠"强制 dark"才能显示。
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface TimelineNode {
  label: string;
  revealFrame: number;
  prefix: string;
  target: number;
  unit: string;
  isTotal?: boolean;
}

export interface TimelineSectionProps {
  heading: string;
  mountFrame: number;
  endFrame: number;
  nodes: TimelineNode[];
  colorMode?: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

const TRACK_X = 340;
// Cleared below SectionLayer's header (title at y=360) and icon zone
// (icon top=500, ~238px tall) -- a Section carrying a `timeline` should
// omit its `icon` so nothing above competes for this space.
const TRACK_TOP = 620;
// Kept clear of the caption band (captions can occupy roughly y=1677-1830
// for a 2-line phrase) -- confirmed via the sandbox build's own render test
// where a TRACK_BOTTOM of 1780 put the last node's text directly behind the
// caption bar.
const TRACK_BOTTOM = 1500;
const GROW_WINDOW = 25;

export const TimelineSection: React.FC<TimelineSectionProps> = ({
  heading,
  mountFrame,
  endFrame,
  nodes,
  colorMode = "dark",
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = PALETTES[colorMode];
  const ACCENT = palette.accent;
  const ACCENT_ALT = palette.accentAlt;
  const LINE = palette.line;
  const BG = palette.bg;
  const SHADOW = palette.shadow;
  // Node label/heading text: white only makes sense on the dark canvas —
  // warm mode needs the same dark ink every other warm-mode component uses.
  const TEXT = colorMode === "dark" ? "#FFFFFF" : palette.ink;
  const TEXT_SOFT = colorMode === "dark" ? "rgba(255,255,255,0.7)" : palette.inkSoft;

  if (frame < mountFrame || frame >= endFrame || nodes.length < 2) return null;

  const exitFade = interpolate(frame, [endFrame - 15, endFrame], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const entryFade = interpolate(frame, [mountFrame, mountFrame + 15], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const step = (TRACK_BOTTOM - TRACK_TOP) / (nodes.length - 1);

  // Fill-line height builds progressively: holds flat, then grows to the
  // next cumulative height in the GROW_WINDOW frames right before each
  // node's own reveal frame, so the line "arrives" as the dot pops.
  //
  // Built defensively, not trusting revealFrame spacing: once a video cut
  // (src/cuts.ts) can shift these frames, two nodes can end up close enough
  // together that `revealFrame - GROW_WINDOW` for the later one lands at or
  // before an already-pushed frame — interpolate() hard-throws on a
  // non-strictly-increasing input range. Clamping every pushed frame to at
  // least `prev + 1` guarantees strictly increasing regardless of how tight
  // the spacing gets (a cut landing between two nodes is exactly this case,
  // and mapKeyframes' own collapse-dedupe only catches EXACT ties, not
  // "too close together").
  const fillFrames: number[] = [nodes[0].revealFrame];
  const fillHeights: number[] = [0];
  for (let i = 1; i < nodes.length; i++) {
    const growStart = Math.max(fillFrames[fillFrames.length - 1] + 1, nodes[i].revealFrame - GROW_WINDOW);
    const growEnd = Math.max(growStart + 1, nodes[i].revealFrame);
    fillFrames.push(growStart, growEnd);
    fillHeights.push(step * (i - 1), step * i);
  }
  const fillHeight = interpolate(frame, fillFrames, fillHeights, {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <div style={{ position: "absolute", inset: 0, opacity: entryFade * exitFade }}>
      <div
        style={{
          position: "absolute",
          left: 0,
          top: 480,
          width: 1080,
          textAlign: "center",
          fontFamily: headingFont,
          fontSize: 30,
          fontWeight: 800,
          letterSpacing: 3,
          color: ACCENT,
        }}
      >
        {heading.toUpperCase()}
      </div>

      {/* Base track (always visible once mounted) */}
      <div
        style={{
          position: "absolute",
          left: TRACK_X,
          top: TRACK_TOP,
          width: 6,
          height: TRACK_BOTTOM - TRACK_TOP,
          borderRadius: 3,
          background: LINE,
        }}
      />
      {/* Accent fill, grows node by node -- gradient (same hues as the
          global RainbowProgressBar's first two stops), not a flat color. */}
      <div
        style={{
          position: "absolute",
          left: TRACK_X,
          top: TRACK_TOP,
          width: 6,
          height: fillHeight,
          borderRadius: 3,
          background: `linear-gradient(180deg,${ACCENT},${ACCENT_ALT})`,
        }}
      />

      {nodes.map((node, i) => {
        const y = TRACK_TOP + step * i;
        const local = frame - node.revealFrame;
        if (local < 0) return null;

        const dotScale = spring({ frame: local, fps, config: { damping: 13, stiffness: 200 } });
        const textOpacity = interpolate(local, [0, 20], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });
        const textSlide = interpolate(local, [0, 20], [-30, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });
        const value = interpolate(local, [0, 30], [0, node.target], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });
        const displayValue = `${node.prefix}${Math.round(value)}`;
        const dotSize = node.isTotal ? 34 : 24;
        const dotColor = node.isTotal ? ACCENT_ALT : ACCENT;

        return (
          <div key={node.label}>
            <div
              style={{
                position: "absolute",
                left: TRACK_X + 3 - dotSize / 2,
                top: y - dotSize / 2,
                width: dotSize,
                height: dotSize,
                borderRadius: dotSize / 2,
                background: dotColor,
                border: `4px solid ${BG}`, // clean cutout against the dark canvas
                boxShadow: `0 4px 14px ${SHADOW}`,
                transform: `scale(${dotScale})`,
              }}
            />
            <div
              style={{
                position: "absolute",
                left: TRACK_X + 46,
                top: y - (node.isTotal ? 54 : 40),
                opacity: textOpacity,
                transform: `translateX(${textSlide}px)`,
              }}
            >
              <div
                style={{
                  fontFamily: labelFont,
                  fontSize: node.isTotal ? 34 : 28,
                  fontWeight: 800,
                  letterSpacing: 2,
                  color: TEXT,
                  marginBottom: 4,
                }}
              >
                {node.label}
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  gap: 10,
                }}
              >
                <span
                  style={{
                    fontFamily: headingFont,
                    fontSize: node.isTotal ? 70 : 52,
                    fontWeight: 800,
                    color: dotColor,
                    lineHeight: 1,
                  }}
                >
                  {displayValue}
                </span>
                <span
                  style={{
                    fontFamily: labelFont,
                    fontSize: 24,
                    fontWeight: 700,
                    color: TEXT_SOFT,
                    letterSpacing: 1,
                  }}
                >
                  {node.unit}
                </span>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};
