/**
 * StepList — numbered step rows shown as a full skeleton from mount, each
 * row activating (ghosted -> fully rendered, spring pop) at its own spoken
 * beat. Matches video-studio's dajaai-walking-fresh reference ("numbered
 * step list with full skeleton ghosted, activating one row per spoken
 * beat"). Distinct from InfoCard's rows (which all animate in together via
 * a fixed per-index stagger) — here each row's activation frame is
 * independently driven by when that step is actually spoken, and inactive
 * rows stay visibly present (just faint), giving the viewer a sense of
 * "here's the whole list, we're on step 2 of 4" instead of building blind.
 */
import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { interpolate } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface StepListStep {
  label: string;
  labelEn?: string;
  sub?: string;
  /** Frame (relative to the card's own mountFrame) this step activates. */
  activateOffset: number;
}

export interface StepListProps {
  title?: string;
  steps: StepListStep[];
  mountFrame: number;
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const StepList: React.FC<StepListProps> = ({
  title,
  steps,
  mountFrame,
  endFrame,
  x = 60,
  y = 1040,
  width = 960,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;
  if (endFrame !== undefined && frame >= endFrame) return null;
  const exitFade = endFrame !== undefined
    ? interpolate(frame, [endFrame - 15, endFrame], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  const palette = PALETTES[colorMode];
  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 200 } });

  return (
    <div style={{ position: "absolute", left: x, top: y, width, opacity: cardEntry * exitFade }}>
      {title ? (
        <div style={{
          fontFamily: headingFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginBottom: 14,
        }}>
          {title}
        </div>
      ) : null}
      {steps.map((step, i) => {
        const stepLocal = local - step.activateOffset;
        const active = stepLocal >= 0;
        const pop = active
          ? spring({ frame: Math.min(stepLocal, 30), fps, config: { damping: 14, stiffness: 220 } })
          : 0;
        return (
          <div
            key={i}
            style={{
              display: "flex", alignItems: "center", gap: 18,
              background: active ? palette.card : "transparent",
              border: active ? "none" : `1.5px solid ${palette.line}`,
              borderRadius: 16,
              padding: "18px 22px",
              marginBottom: 12,
              // Fix C32 (2026-07-20, vision QA caught it directly on a real render,
              // job_51f154a80f9b: "inactive step text barely readable against the
              // light background"): inactive rows used to stack TWO independent
              // dimming mechanisms — palette.inkSoft (already a muted color vs.
              // palette.ink) on the text, AND a 0.32 opacity cut on the whole row.
              // Multiplied together against the warm palette's light cream bg
              // (#F2EBE0), inkSoft's own already-modest contrast collapsed to
              // near-invisible. The border-vs-solid-background and outline-vs-
              // filled circle already clearly signal "not yet active" — text
              // legibility shouldn't be sacrificed on top of that for the same
              // signal. Row opacity now only takes the edge off, not the color.
              opacity: active ? 1 : 0.88,
              boxShadow: active ? `0 8px 24px ${palette.shadow}` : "none",
              transform: `scale(${active ? 0.97 + 0.03 * pop : 1})`,
            }}
          >
            <div
              style={{
                width: 44, height: 44, borderRadius: "50%", flexShrink: 0,
                background: active ? palette.accent : "transparent",
                border: active ? "none" : `1.5px solid ${palette.inkSoft}`,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontFamily: headingFont, fontSize: 20, fontWeight: 800,
                color: active ? "#FFFFFF" : palette.inkSoft,
              }}
            >
              {i + 1}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span style={{
                fontFamily: labelFont, fontSize: 24, fontWeight: 700,
                color: active ? palette.ink : palette.inkSoft,
              }}>
                {step.label}
              </span>
              {step.labelEn || step.sub ? (
                <span style={{
                  fontFamily: labelFont, fontSize: 13, fontWeight: 600, letterSpacing: 1.5,
                  color: palette.inkSoft, textTransform: "uppercase",
                }}>
                  {step.labelEn ?? step.sub}
                </span>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
};
