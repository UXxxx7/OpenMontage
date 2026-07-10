/**
 * QuoteCard — pull-quote typography moment for videos with NO hard data.
 *
 * The graphic vocabulary (InfoCard/gauge/countdown/calendar) only fires on
 * numeric/date/risk moments; a casual talking video planned zero of those
 * and rendered as bare card+captions for its whole middle (observed on a
 * real job). This is the codex's answer for那类内容: promote 1-3 actually-
 * spoken key sentences to full typography moments — big quoted line, accent
 * quote mark, word-group staggered reveal — so a data-less video still gets
 * canvas-level motion anchored to its own words.
 *
 * [P2 prototype validating the contract-② `quotes` extension; P3 owns the
 * final form.]
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { APPLE, ColorMode, PALETTES } from "./theme";

export interface QuoteCardProps {
  /** The spoken line, verbatim (planner must not invent). */
  text: string;
  /** Optional speaker/source line under the quote. */
  attribution?: string;
  mountFrame: number;
  /** Frame the card starts a 15-frame fade-out. */
  endFrame?: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const QuoteCard: React.FC<QuoteCardProps> = ({
  text,
  attribution,
  mountFrame,
  endFrame,
  x = 90,
  y = 480,
  width = 900,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;
  if (endFrame !== undefined && frame >= endFrame) return null;

  const palette = PALETTES[colorMode];
  const exitFade =
    endFrame !== undefined
      ? interpolate(frame, [endFrame - 15, endFrame], [1, 0], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp",
        })
      : 1;

  const markEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 220 } });
  // Word-group staggered reveal — 3 frames per word, capped so long lines
  // still land within ~1.2s.
  const words = text.split(/\s+/).filter(Boolean);
  const perWord = Math.min(3, 36 / Math.max(1, words.length));
  // Sweep underline after the text lands
  const sweepStart = 10 + words.length * perWord;
  const sweep = interpolate(local, [sweepStart, sweepStart + 20], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
  });

  const isCjk = /[一-鿿]/.test(text);
  const fontSize = text.length > 40 ? 44 : text.length > 20 ? 54 : 64;

  return (
    <div
      style={{
        position: "absolute", left: x, top: y, width,
        display: "flex", flexDirection: "column", alignItems: "center",
        opacity: exitFade, textAlign: "center",
      }}
    >
      <span
        style={{
          fontFamily: headingFont, fontSize: 120, fontWeight: 800, lineHeight: 0.6,
          color: palette.accent, opacity: markEntry * 0.9,
          transform: `scale(${0.7 + markEntry * 0.3})`, marginBottom: 18,
        }}
      >
        “
      </span>
      <div style={{ fontFamily: headingFont, fontSize, fontWeight: 800, lineHeight: 1.3, color: palette.ink }}>
        {isCjk
          ? // CJK: char-group reveal (5 chars per group keeps the same cadence)
            Array.from(text).map((ch, i) => {
              const gi = Math.floor(i / 5);
              const o = interpolate(local, [8 + gi * perWord * 5, 16 + gi * perWord * 5], [0, 1], {
                extrapolateLeft: "clamp", extrapolateRight: "clamp",
              });
              return <span key={i} style={{ opacity: o }}>{ch}</span>;
            })
          : words.map((w, i) => {
              const o = interpolate(local, [8 + i * perWord, 16 + i * perWord], [0, 1], {
                extrapolateLeft: "clamp", extrapolateRight: "clamp",
              });
              return (
                <span key={i} style={{ opacity: o, display: "inline-block", marginRight: "0.28em" }}>
                  {w}
                </span>
              );
            })}
      </div>
      <div
        style={{
          height: 3, width: sweep * 220, marginTop: 26,
          background: `linear-gradient(90deg, ${palette.accent}, ${palette.accentAlt})`,
        }}
      />
      {attribution ? (
        <span
          style={{
            fontFamily: labelFont, fontSize: 20, fontWeight: 600, letterSpacing: 3,
            color: palette.inkSoft, marginTop: 20, textTransform: "uppercase",
            opacity: interpolate(local, [sweepStart + 10, sweepStart + 24], [0, 1], {
              extrapolateLeft: "clamp", extrapolateRight: "clamp",
            }),
          }}
        >
          {attribution}
        </span>
      ) : null}
    </div>
  );
};
