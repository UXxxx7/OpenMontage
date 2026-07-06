/**
 * InfoCard — dark ink panel with pill rows. "The most important overlay
 * component" per compose-director.md. Rows with a numeric `value` count up
 * from 0 rather than appearing as static text, per the Data Display
 * Analysis rule ("if a number is spoken, count it up").
 *
 * Beat anchoring convention from compose-director.md: mount this at
 * `keyword_frame - 50` (50 frames early), not exactly on the spoken word.
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLOR } from "./theme";

export type Tone = "accent" | "good" | "bad" | "normal";

const TONE_COLOR: Record<Tone, string> = {
  accent: COLOR.terracotta,
  good: COLOR.good,
  bad: COLOR.bad,
  normal: "#FFFFFF",
};

export interface InfoRow {
  label: string;
  /** Numeric target for count-up. Omit for a purely textual row. */
  value?: number;
  /** Formats the (possibly still-animating) numeric value for display. Not JSON-serializable — use prefix/divideBy/decimals from a JSON props file instead. */
  formatValue?: (v: number) => string;
  /** Prepended to the formatted number, e.g. "$". JSON-friendly alternative to formatValue. */
  prefix?: string;
  /** Divides the animating value before display, e.g. 1000000 + decimals=1 -> "1.5" for 1,500,000. */
  divideBy?: number;
  decimals?: number;
  /** Used verbatim instead of `value` when there's nothing to count up. */
  staticValue?: string;
  unit?: string;
  tone?: Tone;
  /**
   * Frames after the card's own mountFrame this row starts entering.
   * Defaults to a 6-frame stagger per row index if omitted — set explicitly
   * when rows correspond to data points spoken far apart in time (e.g. two
   * numbers 2s apart in speech), so each row's count-up lands on its own
   * spoken beat instead of all rows firing in a tight 6-frame cascade.
   */
  mountOffset?: number;
}

export interface InfoCardProps {
  title: string;
  subtitle?: string;
  rows: InfoRow[];
  x: number;
  y: number;
  width: number;
  /** Frame this card starts entering. Caller should set this to keyword_frame - 50. */
  mountFrame: number;
  font?: string;
}

const defaultFormat = (v: number, decimals = 0) =>
  decimals > 0 ? v.toFixed(decimals) : Math.round(v).toLocaleString();

export const InfoCard: React.FC<InfoCardProps> = ({
  title,
  subtitle,
  rows,
  x,
  y,
  width,
  mountFrame,
  font = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;

  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 220 } });

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        background: "rgba(13,17,23,0.86)",
        borderRadius: 16,
        padding: "20px 24px",
        boxShadow: "0 8px 32px rgba(0,0,0,0.32)",
        opacity: cardEntry,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
      }}
    >
      <div style={{ fontFamily: font, fontSize: 11, fontWeight: 600, letterSpacing: 3, color: "rgba(255,255,255,0.55)", marginBottom: 4 }}>
        {title.toUpperCase()}
      </div>
      {subtitle ? (
        <div style={{ fontFamily: font, fontSize: 15, fontWeight: 700, letterSpacing: 2, color: "#FFFFFF", marginBottom: 10 }}>
          {subtitle}
        </div>
      ) : null}
      {rows.map((row, i) => {
        const rowLocal = local - (row.mountOffset ?? i * 6);
        const rowEntry = spring({ frame: Math.max(0, rowLocal), fps, config: { damping: 14, stiffness: 220 } });
        if (rowLocal < 0) return null;
        const tone = TONE_COLOR[row.tone ?? "normal"];
        const animatedValue =
          row.value !== undefined
            ? interpolate(rowLocal, [0, 40], [0, row.value], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
            : undefined;
        const displayValue =
          animatedValue === undefined
            ? row.staticValue ?? ""
            : row.formatValue
            ? row.formatValue(animatedValue)
            : `${row.prefix ?? ""}${defaultFormat(
                row.divideBy ? animatedValue / row.divideBy : animatedValue,
                row.decimals
              )}`;

        return (
          <div
            key={row.label}
            style={{
              background: "rgba(255,255,255,0.06)",
              borderRadius: 10,
              padding: "12px 16px",
              marginBottom: 8,
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              opacity: rowEntry,
              transform: `translateX(${(1 - rowEntry) * -40}px)`,
            }}
          >
            <span style={{ fontFamily: font, fontSize: 18, fontWeight: 700, color: "#FFFFFF" }}>{row.label}</span>
            <span>
              <span style={{ fontFamily: font, fontSize: 26, fontWeight: 800, color: tone }}>{displayValue}</span>
              {row.unit ? (
                <span style={{ fontFamily: font, fontSize: 12, fontWeight: 600, color: "#FFFFFF", opacity: 0.7, marginLeft: 4 }}>
                  {row.unit}
                </span>
              ) : null}
            </span>
          </div>
        );
      })}
    </div>
  );
};
