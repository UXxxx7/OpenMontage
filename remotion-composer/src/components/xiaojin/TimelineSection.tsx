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
 * Colors are hardcoded (not read from theme.ts's colorMode palettes) --
 * this component is designed to fill a Section takeover's dark canvas (see
 * SectionLayer, which renders it), and the sandbox build's own theme.ts
 * experiment (overriding the SHARED dark palette's accent to this
 * red-orange) was reverted before porting: that palette is also the
 * documented brand "dark mode" for other xiaojin videos (electric blue,
 * see CLAUDE.md's style doc), and overriding it globally would have changed
 * every other dark-mode composition's accent color. Hardcoding here keeps
 * the change scoped to this one component, matching the pattern its sibling
 * components (StatCard/BudgetRevealSection) already use.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

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
  headingFont?: string;
  labelFont?: string;
}

// Matches production theme.ts's unmodified `dark` palette values exactly
// (bg/line/shadow untouched there) plus this component's own red-orange
// accent (see doc comment above for why that isn't in the shared palette).
const ACCENT = "#E0552F";
const ACCENT_ALT = "#E8A13C";
const LINE = "rgba(255,255,255,0.10)";
const BG = "#0D1117";
const SHADOW = "rgba(0,0,0,0.45)";

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
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

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
  const fillFrames: number[] = [nodes[0].revealFrame];
  const fillHeights: number[] = [0];
  for (let i = 1; i < nodes.length; i++) {
    fillFrames.push(nodes[i].revealFrame - GROW_WINDOW, nodes[i].revealFrame);
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
                  color: "#FFFFFF",
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
                    color: "rgba(255,255,255,0.7)",
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
