/**
 * Mini-calendar with a pulsing target-date marker — "Specific date" row of
 * compose-director.md's Data Display Analysis table ("Mini calendar, target
 * date circled with pulse animation").
 *
 * Ported from video-studio's motion/vell-renewal-fresh/src/RenewalFresh/IntroSection.tsx
 * (lines 6-15, 110-181) — generalized. The two existing references in
 * video-studio (this one, and the sibling motion/vell-renewal-reminder's
 * CalendarGraphic.tsx) both hardcode a literal July-2025 day-grid array with
 * no real date math anywhere. This version computes the actual grid from
 * `year`/`month` (first-weekday-of-month, days-in-month), so it works for any
 * month/year a real job's content-plan gives it, not just the one video both
 * references were built for.
 */
import { useCurrentFrame, useVideoConfig, spring, interpolate } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface CalendarProps {
  /** Full year, e.g. 2026. */
  year: number;
  /** 1-12. */
  month: number;
  targetDay: number;
  /** Optional "today" marker day, same month. Omit to skip. */
  todayDay?: number;
  /** e.g. "Renewal Deadline" — the component appends "— <Month> <targetDay>". */
  eventLabel: string;
  mountFrame: number;
  /** Frame this card starts fading out (15-frame fade). Omit to stay on screen for the rest of the video. */
  endFrame?: number;
  x?: number;
  y?: number;
  /** Card width. Default bumped 460->720 (confirmed real user complaint: at
      460px inside the ~960px content-zone lane, this was the single most
      undersized component in the whole build, leaving huge empty margins —
      "biggen the calendar, that's one technique to remove empty space"). */
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const DAY_HEADERS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

/** Real calendar math — first weekday of the month (0=Sun) and days-in-month
 * via the "day 0 of next month" trick — not a hardcoded grid. */
function buildMonthGrid(year: number, month: number): (number | null)[][] {
  const firstWeekday = new Date(year, month - 1, 1).getDay();
  const daysInMonth = new Date(year, month, 0).getDate();

  const cells: (number | null)[] = [];
  for (let i = 0; i < firstWeekday; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);
  while (cells.length % 7 !== 0) cells.push(null);

  const weeks: (number | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

export const Calendar: React.FC<CalendarProps> = ({
  year,
  month,
  targetDay,
  todayDay,
  eventLabel,
  mountFrame,
  endFrame,
  x = 80,
  y = 900,
  width = 720,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;
  if (endFrame !== undefined && frame >= endFrame) return null;
  const exitFade =
    endFrame !== undefined
      ? interpolate(frame, [endFrame - 15, endFrame], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
      : 1;

  const palette = PALETTES[colorMode];
  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 200 } });
  const pulse = Math.sin(local / 8) * 0.5 + 0.5;

  const weeks = buildMonthGrid(year, month);

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        opacity: cardEntry * exitFade,
        transform: `translateX(${(1 - cardEntry) * -40}px)`,
        background: palette.card,
        borderRadius: 24,
        border: `1.5px solid ${palette.line}`,
        boxShadow: `0 8px 32px ${palette.shadow}`,
        padding: "32px 36px",
        width,
      }}
    >
      <div
        style={{
          fontFamily: headingFont,
          fontSize: 32,
          fontWeight: 800,
          color: palette.ink,
          marginBottom: 20,
          letterSpacing: 1,
        }}
      >
        {MONTH_NAMES[month - 1]} {year}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 6, marginBottom: 10 }}>
        {DAY_HEADERS.map((d) => (
          <div
            key={d}
            style={{
              textAlign: "center",
              fontFamily: labelFont,
              fontSize: 18,
              fontWeight: 700,
              color: palette.inkSoft,
              letterSpacing: 1,
            }}
          >
            {d}
          </div>
        ))}
      </div>

      {weeks.map((week, wi) => (
        <div key={wi} style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 6, marginBottom: 6 }}>
          {week.map((day, di) => {
            const isTarget = day === targetDay;
            const isToday = todayDay !== undefined && day === todayDay;
            return (
              <div
                key={di}
                style={{
                  textAlign: "center",
                  fontFamily: labelFont,
                  fontSize: 26,
                  fontWeight: isTarget ? 800 : 600,
                  color: isTarget ? "#FFFFFF" : day === null ? "transparent" : palette.ink,
                  padding: "12px 4px",
                  borderRadius: isTarget ? "50%" : 10,
                  background: isTarget ? palette.accent : isToday ? "rgba(196,113,74,0.10)" : "transparent",
                  boxShadow: isTarget
                    ? `0 0 0 ${3 + pulse * 4}px ${palette.accent}${Math.round((0.25 + pulse * 0.25) * 255).toString(16).padStart(2, "0")}`
                    : undefined,
                }}
              >
                {day ?? ""}
              </div>
            );
          })}
        </div>
      ))}

      <div
        style={{
          marginTop: 16,
          display: "flex",
          alignItems: "center",
          gap: 12,
          paddingTop: 16,
          borderTop: `1px solid ${palette.line}`,
        }}
      >
        <div style={{ width: 16, height: 16, borderRadius: "50%", background: palette.accent, flexShrink: 0 }} />
        <span style={{ fontFamily: labelFont, fontSize: 22, fontWeight: 700, color: palette.accent }}>
          {eventLabel} — {MONTH_NAMES[month - 1]} {targetDay}
        </span>
      </div>
    </div>
  );
};
