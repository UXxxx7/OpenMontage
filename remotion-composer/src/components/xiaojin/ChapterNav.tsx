/**
 * Persistent top chapter navigation bar — updates as the current section
 * changes. Ported from video-studio's ChapterNav.tsx; generalized to take
 * `chapters` as a prop instead of importing a per-project generated timeline.
 */
import { interpolate, useCurrentFrame } from "remotion";
import { ColorMode, NAV, PALETTES, W } from "./theme";

export interface Chapter {
  /** Frame this chapter becomes active. */
  atFrame: number;
  label: string;
  labelEn?: string;
}

export interface ChapterNavProps {
  chapters: Chapter[];
  /** Frame the intro ends and the nav bar should fade in. */
  introOutFrame: number;
  colorMode?: ColorMode;
  headingFont?: string;
  labelFont?: string;
  /** Editor-only position override — omit for the default pinned-top,
   *  full-width placement (NAV.h from theme.ts stays the height regardless). */
  x?: number;
  y?: number;
  width?: number;
}

export const ChapterNav: React.FC<ChapterNavProps> = ({
  chapters,
  introOutFrame,
  colorMode = "warm",
  headingFont = "inherit",
  labelFont = "inherit",
  x = 0,
  y = 0,
  width = W,
}) => {
  const frame = useCurrentFrame();
  const palette = PALETTES[colorMode];

  const opacity = interpolate(frame, [introOutFrame, introOutFrame + 16], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  let active = 0;
  chapters.forEach((c, i) => {
    if (frame >= c.atFrame) active = i;
  });

  if (chapters.length === 0) return null;

  return (
    <div
      style={{
        position: "absolute",
        top: y,
        left: x,
        width,
        height: NAV.h,
        opacity,
        background:
          colorMode === "warm" ? "rgba(255,255,255,0.60)" : "rgba(13,17,23,0.72)",
        backdropFilter: "blur(8px)",
        borderBottom: `1.5px solid ${palette.line}`,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 48,
      }}
    >
      {chapters.map((c, i) => {
        const on = i === active;
        return (
          <div
            key={`${c.label}-${i}`}
            style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}
          >
            <span
              style={{
                fontFamily: headingFont,
                fontSize: 26,
                fontWeight: on ? 700 : 500,
                color: on ? palette.accent : palette.inkSoft,
              }}
            >
              {c.label}
            </span>
            {c.labelEn ? (
              <span
                style={{
                  fontFamily: labelFont,
                  fontSize: 11,
                  letterSpacing: 1.8,
                  fontWeight: 700,
                  color: on ? palette.accent : palette.inkSoft,
                  opacity: on ? 1 : 0.55,
                }}
              >
                {c.labelEn}
              </span>
            ) : null}
            {on && (
              <div
                style={{
                  marginTop: 2,
                  width: 28,
                  height: 3,
                  borderRadius: 2,
                  background: palette.accent,
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
};
