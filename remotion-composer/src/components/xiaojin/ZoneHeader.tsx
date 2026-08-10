/**
 * ZoneHeader — compact section header for normal (non-takeover) chapters,
 * rendered at the top of the content zone while that chapter's data is on
 * screen. Takeover chapters already get a big centered header via
 * SectionLayer; this is the equivalent move for ordinary chapters, matching
 * video-studio's dajaai-walking-fresh reference ("card top -> big section
 * header below card"). Left-aligned (not centered like SectionLayer's,
 * which owns the whole canvas) since this sits directly under the docked
 * SpeakerCard, sharing the content zone's left margin with the cards below.
 *
 * content_planner.py reserves _ZONE_HEADER_HEIGHT of vertical space above
 * the content-zone stack for any chapter this renders in, so cards below
 * never overlap it — the y this receives is already offset accordingly.
 */
import { interpolate, useCurrentFrame } from "remotion";
import { APPLE, ColorMode, PALETTES } from "./theme";

export interface ZoneHeaderProps {
  title: string;
  titleEn?: string;
  fromFrame: number;
  toFrame: number;
  x?: number;
  y?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

const ENTER_FRAMES = 18;
const EXIT_FRAMES = 15;

export const ZoneHeader: React.FC<ZoneHeaderProps> = ({
  title,
  titleEn,
  fromFrame,
  toFrame,
  x = 60,
  y = 1040,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  if (frame < fromFrame || frame >= toFrame + EXIT_FRAMES) return null;
  const local = frame - fromFrame;
  const palette = PALETTES[colorMode];

  const enter = interpolate(local, [0, ENTER_FRAMES], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
  });
  const exit = interpolate(frame, [toFrame, toFrame + EXIT_FRAMES], [1, 0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: APPLE,
  });
  const opacity = Math.min(enter, exit);

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        opacity,
        transform: `translateY(${(1 - enter) * 14}px)`,
      }}
    >
      <div style={{ fontFamily: headingFont, fontSize: 42, fontWeight: 800, color: palette.ink, lineHeight: 1.1 }}>
        {title}
      </div>
      {titleEn ? (
        <div style={{
          fontFamily: labelFont, fontSize: 16, fontWeight: 600, letterSpacing: 3,
          color: palette.inkSoft, textTransform: "uppercase", marginTop: 4,
        }}>
          {titleEn}
        </div>
      ) : null}
      <div style={{ width: 64, height: 3, marginTop: 10, background: palette.accent, borderRadius: 2 }} />
    </div>
  );
};
