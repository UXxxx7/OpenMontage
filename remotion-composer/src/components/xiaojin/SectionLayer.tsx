/**
 * SectionLayer — full-canvas chapter takeover, generalized from the
 * hand-built sections in video-studio's vell-renewal-reminder
 * (CoverageSection/LapseSection/...). This is the biggest visual-vocabulary
 * gap between the hand-crafted reference and the JSON-driven template:
 * the reference's signature move is that a chapter OWNS the whole canvas
 * (its own background — often dark — plus an eyebrow/title header block and
 * a section icon), with the speaker card docked as a small pip.
 *
 * JSON-driven equivalent: each `Section` describes one takeover span.
 * Layout constants mirror the reference exactly (header at y=360,
 * terracotta eyebrow letterSpacing 7 / big 800-weight title). Below the
 * header, a takeover either shows TimelineSection (when `timeline` is set)
 * or a body-fill zone (icon + soft accent backdrop, scaled/positioned to
 * occupy the same y=520-1500 band TimelineSection uses) — P3: a plain
 * title+small-icon takeover used to leave most of the canvas empty below y~760.
 *
 * Layering contract: render this BEFORE ContentZone/graphics/SpeakerCard —
 * background first, data graphics and the card sit on top (reference's
 * index.tsx does the same: "Section content renders first (behind the
 * card)"). Chrome (nav/captions/bars) stays above everything as always.
 *
 * [Prototyped by P2 to validate the contract-② `sections` extension;
 * P3 owns the final form of this file.]
 */
import { interpolate, useCurrentFrame } from "remotion";
import { SECTION_ICONS } from "./SectionIcons";
import { APPLE, ColorMode, PALETTES } from "./theme";
import { TimelineNode, TimelineSection as TimelineSectionGraphic } from "./TimelineSection";

export interface Section {
  /** Takeover span (frames). fromFrame should be >= introOutFrame. */
  fromFrame: number;
  toFrame: number;
  /** Small terracotta letterspaced line above the title, e.g. "YOUR COVERAGE". */
  eyebrow?: string;
  /** Big header line in the video's primary language, e.g. "你的保障". */
  title?: string;
  /** Icon name from SECTION_ICONS ("shield_check" | "warning" | "clock"). Omit when `timeline` is set — they occupy the same vertical space. */
  icon?: string;
  /** Overrides the base colorMode for this span (the reference alternates dark/warm). */
  colorMode?: ColorMode;
  /** Warning styling — accents go red instead of terracotta (reference's "why" section). */
  warn?: boolean;
  /**
   * Full-canvas multi-stage process timeline filling the rest of this
   * section's takeover (see TimelineSection). Fix D6（2026-07-16）：used to
   * only render when this section's resolved colorMode was "dark" —
   * TimelineSection is now palette-aware and renders correctly in warm mode
   * too, so this works regardless of colorMode.
   */
  timeline?: {
    heading: string;
    nodes: TimelineNode[];
  };
}

const ENTER_FRAMES = 18;
const EXIT_FRAMES = 16;

export const SectionLayer: React.FC<{
  sections: Section[];
  baseColorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}> = ({ sections, baseColorMode, headingFont = "inherit", labelFont = "inherit" }) => {
  const frame = useCurrentFrame();

  return (
    <>
      {sections.map((s, i) => {
        if (frame < s.fromFrame || frame >= s.toFrame + EXIT_FRAMES) return null;
        const local = frame - s.fromFrame;
        const mode = s.colorMode || baseColorMode;
        const palette = PALETTES[mode];
        const accent = s.warn ? palette.bad : palette.accent;
        const titleColor = mode === "dark" ? "#FFFFFF" : palette.ink;

        const enter = interpolate(local, [0, ENTER_FRAMES], [0, 1], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
        });
        const exit = interpolate(frame, [s.toFrame, s.toFrame + EXIT_FRAMES], [1, 0], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
        });
        const opacity = Math.min(enter, exit);
        const Icon = s.icon ? SECTION_ICONS[s.icon] : undefined;

        return (
          <div key={i} style={{ position: "absolute", inset: 0, opacity }}>
            {/* Background takeover — only when this section overrides the base mode;
                otherwise the base canvas already is the right color. */}
            {s.colorMode && s.colorMode !== baseColorMode ? (
              <div style={{ position: "absolute", inset: 0, background: palette.bg }} />
            ) : null}

            {(s.eyebrow || s.title) && (
              <div
                style={{
                  position: "absolute", left: 0, top: 360, width: "100%",
                  display: "flex", flexDirection: "column", alignItems: "center", gap: 8,
                  transform: `translateY(${(1 - enter) * 18}px)`,
                }}
              >
                {s.eyebrow ? (
                  <span style={{
                    fontFamily: labelFont, fontSize: 14, letterSpacing: 7,
                    fontWeight: 700, color: accent, textTransform: "uppercase",
                  }}>
                    {s.eyebrow}
                  </span>
                ) : null}
                {s.title ? (
                  <span style={{
                    fontFamily: headingFont, fontSize: 50, fontWeight: 800,
                    color: titleColor, lineHeight: 1,
                  }}>
                    {/* codex Animation Vocabulary: section header text uses a
                        typing reveal (char-by-char), not a plain fade. */}
                    {Array.from(s.title).map((ch, ci) => (
                      <span key={ci} style={{
                        opacity: local >= 6 + ci * 4 ? 1 : 0,
                      }}>{ch}</span>
                    ))}
                    <span style={{
                      opacity: local < 6 + s.title.length * 4 + 12 && Math.floor(local / 8) % 2 === 0 ? 0.8 : 0,
                      color: accent, fontWeight: 400,
                    }}>|</span>
                  </span>
                ) : null}
                {/* sweep divider — width animates 0 -> 180 after the title lands */}
                <div style={{
                  height: 3, marginTop: 14,
                  width: Math.max(0, Math.min(1, (local - (8 + (s.title?.length || 0) * 4)) / 18)) * 180,
                  background: `linear-gradient(90deg, ${accent}, transparent)`,
                }} />
              </div>
            )}

            {s.timeline ? (
              <TimelineSectionGraphic
                heading={s.timeline.heading}
                mountFrame={s.fromFrame}
                endFrame={s.toFrame}
                nodes={s.timeline.nodes}
                colorMode={mode}
                headingFont={headingFont}
                labelFont={labelFont}
              />
            ) : (
              /* Centered full-width icon zone. History: this zone bounced
                 between full-canvas-centered (overlapped the old side-docked
                 SpeakerCard pip) and left-column-constrained (dodged the pip
                 but read as visibly OFF-CENTER — confirmed user complaint
                 about the warning icon "just being like that"). The pip is
                 gone now: pipeline_runner hides the SpeakerCard entirely
                 during takeovers via opacityKeyframes, so the graphic owns
                 the whole canvas and centers cleanly. Vertical span 500-1000
                 stays clear of the header block (y=360) above and the
                 content zone (y=1040, where any data card scheduled during
                 the takeover renders) below. */
              <div
                style={{
                  position: "absolute", left: 0, top: 500, width: "100%", height: 500,
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}
              >
                <div
                  style={{
                    position: "absolute", width: 440, height: 440, borderRadius: "50%",
                    background: `radial-gradient(circle, ${accent}26 0%, ${accent}00 72%)`,
                    transform: `scale(${0.9 + 0.1 * enter})`, opacity: enter,
                  }}
                />
                {Icon ? (
                  <div style={{ transform: "scale(2.2)" }}>
                    <Icon localFrame={local} color={accent} />
                  </div>
                ) : null}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
};
