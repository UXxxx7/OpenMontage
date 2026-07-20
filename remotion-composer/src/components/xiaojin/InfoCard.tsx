/**
 * InfoCard — dark ink panel with pill rows. "The most important overlay
 * component" per compose-director.md. Rows with a numeric `value` count up
 * from 0 rather than appearing as static text, per the Data Display
 * Analysis rule ("if a number is spoken, count it up").
 *
 * Ported from postxhs's components/postxhs/InfoCard.tsx (the component
 * contract②'s `dataCards` shape was itself modeled on) — entrance/count-up/
 * stagger logic kept verbatim; tone colors now come from xiaojin's
 * colorMode-aware PALETTES instead of a single hardcoded COLOR set, and
 * font is split into headingFont/labelFont to match XiaojinEditorial's
 * convention.
 *
 * Beat anchoring convention from compose-director.md: mount this at
 * `keyword_frame - 50` (50 frames early), not exactly on the spoken word.
 *
 * Generic, count-up-only by design — no gauge/calendar/QR variant. Those
 * aren't in contract②'s dataCards schema; if a build needs them, that's a
 * contract extension to propose to the team, not something to smuggle in
 * here (see XiaojinEditorial.tsx's outro doc comment for the concrete
 * example that motivated this note — a fixture caption references a QR
 * code with nowhere in the schema to render one).
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export type Tone = "accent" | "good" | "bad" | "normal";

export interface InfoRow {
  label: string;
  /** Optional secondary line under the label — the reference build renders
      bilingual rows (native language big, English small). */
  labelEn?: string;
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
  /** Warning card — title/accents render red (reference's lapse-warning section). */
  warn?: boolean;
  rows: InfoRow[];
  /** Defaults per contract② when omitted. */
  x?: number;
  y?: number;
  width?: number;
  /** Frame this card starts entering. Caller should set this to keyword_frame - 50. */
  mountFrame: number;
  /**
   * Frame this card starts fading out (15-frame fade, fully gone by
   * endFrame). Omit to keep the card on screen for the rest of the video —
   * but note that omitting this on a build with more than one dataCard
   * means every card mounted so far stays visible forever, stacking on top
   * of each other (confirmed real bug: a "From Idea to Upload" card was
   * still fully rendered, unfaded, underneath a later "Video Budget" card
   * because neither InfoCard had any exit logic at all).
   */
  endFrame?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

const defaultFormat = (v: number, decimals = 0) =>
  decimals > 0 ? v.toFixed(decimals) : Math.round(v).toLocaleString();

export const InfoCard: React.FC<InfoCardProps> = ({
  title,
  subtitle,
  rows,
  x = 80,
  y = 900,
  width = 920,
  mountFrame,
  endFrame,
  warn,
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
  // The card's own panel is always a fixed dark color (rgba(13,17,23,0.86)),
  // regardless of the composition's overall warm/dark colorMode — so "normal"
  // must stay a fixed light color too, not palette.ink (which is near-black
  // in warm mode and was nearly invisible against this always-dark panel;
  // confirmed via render test — a row rendered with only its unit visible).
  const toneColor = (tone: Tone = "normal") =>
    tone === "normal" ? "#FFFFFF" : palette[tone];

  const cardEntry = spring({ frame: local, fps, config: { damping: 14, stiffness: 220 } });

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        background: "rgba(13,17,23,0.86)",
        borderLeft: warn ? `4px solid ${palette.bad}` : undefined,
        borderRadius: 16,
        padding: "24px 28px",
        boxShadow: "0 8px 32px rgba(0,0,0,0.32)",
        opacity: cardEntry * exitFade,
        transform: `translateY(${(1 - cardEntry) * 16}px)`,
      }}
    >
      <div style={{ fontFamily: headingFont, fontSize: 13, fontWeight: 600, letterSpacing: 3, color: warn ? palette.bad : "rgba(255,255,255,0.55)", marginBottom: 6 }}>
        {title.toUpperCase()}
      </div>
      {subtitle ? (
        <div style={{ fontFamily: labelFont, fontSize: 18, fontWeight: 700, letterSpacing: 2, color: "#FFFFFF", marginBottom: 12 }}>
          {subtitle}
        </div>
      ) : null}
      {rows.map((row, i) => {
        const rowLocal = local - (row.mountOffset ?? i * 6);
        const rowEntry = spring({ frame: Math.max(0, rowLocal), fps, config: { damping: 14, stiffness: 220 } });
        if (rowLocal < 0) return null;
        const tone = toneColor(row.tone);
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
              padding: "16px 20px",
              marginBottom: 10,
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              opacity: rowEntry,
              transform: `translateX(${(1 - rowEntry) * -40}px)`,
            }}
          >
            <span style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <span style={{ fontFamily: labelFont, fontSize: 26, fontWeight: 700, color: "#FFFFFF" }}>{row.label}</span>
              {row.labelEn ? (
                <span style={{ fontFamily: labelFont, fontSize: 13, fontWeight: 600, letterSpacing: 2, color: "#FFFFFF", opacity: 0.5, textTransform: "uppercase" }}>
                  {row.labelEn}
                </span>
              ) : null}
            </span>
            <span>
              <span style={{ fontFamily: headingFont, fontSize: 52, fontWeight: 800, color: tone, fontVariantNumeric: "tabular-nums" }}>{displayValue}</span>
              {row.unit ? (
                <span style={{ fontFamily: labelFont, fontSize: 18, fontWeight: 600, color: "#FFFFFF", opacity: 0.7, marginLeft: 6 }}>
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
