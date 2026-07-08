/**
 * Topical CTA outro — kicker label, big headline (with an optional
 * accent-colored second line), divider, subtext, a filled CTA pill, and a
 * small footer label. This is the "found footage / style study" outro
 * pattern from chris-quote and iman-watches (their comment: "Same
 * layout/spread convention... spans ~58% of the available zone vertically
 * per the CTA/outro fill rule" — i.e. this satisfies video-studio's
 * pre-render checklist rule that CTA/outro content must not leave a mostly
 * empty zone below it).
 *
 * NOT ported: vell-renewal-reminder's ContactSection + QR-card outro
 * variant (agent name/role/contact + WhatsApp QR code) — that one is a
 * business-data-driven card, not a generic CTA, and needs its own
 * component built against a real fact-sheet schema when a regulated/
 * lead-gen use case actually needs it.
 */
import { interpolate, useCurrentFrame } from "remotion";
import { APPLE, ColorMode, H, PALETTES, W } from "./theme";

export interface OutroSectionProps {
  kicker: string;
  headline: string;
  /** Rendered on its own line in the accent color, below `headline`. */
  headlineAccent?: string;
  subtext: string;
  ctaLabel: string;
  footerLabel: string;
  /** Frame this section starts revealing. */
  outroFromFrame: number;
  colorMode?: ColorMode;
  font?: string;
}

export const OutroSection: React.FC<OutroSectionProps> = ({
  kicker,
  headline,
  headlineAccent,
  subtext,
  ctaLabel,
  footerLabel,
  outroFromFrame,
  colorMode = "warm",
  font = "inherit",
}) => {
  const frame = useCurrentFrame();
  const palette = PALETTES[colorMode];
  if (frame < outroFromFrame) return null;
  const local = frame - outroFromFrame;

  const opts = { extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const, easing: APPLE };
  const reveal = interpolate(local, [8, 30], [0, 1], opts);
  const kickerReveal = interpolate(local, [4, 20], [0, 1], opts);
  const r1 = interpolate(local, [8, 28], [0, 1], opts);
  const r2 = interpolate(local, [20, 40], [0, 1], opts);
  const r3 = interpolate(local, [32, 52], [0, 1], opts);
  const lineW = interpolate(local, [8, 28], [0, 220], opts);

  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        top: 88,
        width: W,
        height: H - 88 - 72,
        background: palette.bg,
        opacity: reveal,
      }}
    >
      <div style={{ position: "absolute", left: 0, top: 260, width: W, textAlign: "center", opacity: kickerReveal }}>
        <span
          style={{
            fontFamily: font,
            fontSize: 15,
            fontWeight: 700,
            letterSpacing: 6,
            color: palette.accent,
            textTransform: "uppercase",
          }}
        >
          {kicker}
        </span>
      </div>

      <div
        style={{
          position: "absolute",
          left: 0,
          top: 420,
          width: W,
          opacity: r1,
          transform: `translateY(${(1 - r1) * 20}px)`,
          textAlign: "center",
          fontFamily: font,
          fontSize: 62,
          fontWeight: 800,
          color: palette.ink,
          lineHeight: 1.15,
          padding: "0 80px",
        }}
      >
        {headline}
        {headlineAccent ? (
          <>
            <br />
            <span style={{ color: palette.accent }}>{headlineAccent}</span>
          </>
        ) : null}
      </div>

      <div
        style={{
          position: "absolute",
          left: (W - 220) / 2,
          top: 760,
          width: lineW,
          height: 3,
          borderRadius: 2,
          background: `linear-gradient(90deg, ${palette.accent}, ${palette.accentAlt})`,
        }}
      />

      <div
        style={{
          position: "absolute",
          left: 0,
          top: 812,
          width: W,
          textAlign: "center",
          opacity: r2,
          transform: `translateY(${(1 - r2) * 16}px)`,
          fontFamily: font,
          fontSize: 26,
          fontWeight: 500,
          color: palette.inkSoft,
        }}
      >
        {subtext}
      </div>

      <div
        style={{
          position: "absolute",
          left: 0,
          top: 930,
          width: W,
          display: "flex",
          justifyContent: "center",
          opacity: r3,
          transform: `translateY(${(1 - r3) * 14}px)`,
        }}
      >
        <div
          style={{
            background: palette.accent,
            color: "#FFFFFF",
            fontFamily: font,
            fontSize: 30,
            fontWeight: 800,
            letterSpacing: 2,
            padding: "22px 56px",
            borderRadius: 40,
          }}
        >
          {ctaLabel}
        </div>
      </div>

      <div style={{ position: "absolute", left: 0, top: 1260, width: W, textAlign: "center", opacity: r3 }}>
        <span
          style={{
            fontFamily: font,
            fontSize: 14,
            fontWeight: 600,
            color: palette.inkSoft,
            opacity: 0.7,
            letterSpacing: 3,
            textTransform: "uppercase",
          }}
        >
          {footerLabel}
        </span>
      </div>
    </div>
  );
};
