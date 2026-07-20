/**
 * QR code + WhatsApp CTA card stack — the contact/lead-gen close, satisfying
 * compose-director.md's rule that a QR code must be a real image
 * (`<Img src={staticFile(...)} />`), never a text placeholder.
 *
 * Ported from video-studio's motion/vell-renewal-fresh/src/RenewalFresh/ContactSection.tsx
 * (lines 79-125) — generalized: contact name/company, QR image path, and CTA
 * copy are now props instead of "David Lee · Pacific Life" hardcoded literals.
 *
 * qrSrc should point at a real generated QR image (see the P2-side QR
 * generation helper, which follows video-studio's own "skip if no real
 * contact info, never invent one" doctrine) — never fabricate a QR image for
 * a contact that wasn't actually supplied.
 */
import { Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { ColorMode, PALETTES } from "./theme";

export interface QRContactCardProps {
  /** Path relative to remotion-composer/public/, e.g. `jobs/<slug>/qr.png`. */
  qrSrc: string;
  contactName: string;
  contactCompany?: string;
  ctaLabel: string;
  mountFrame: number;
  x?: number;
  y?: number;
  width?: number;
  colorMode: ColorMode;
  headingFont?: string;
  labelFont?: string;
}

export const QRContactCard: React.FC<QRContactCardProps> = ({
  qrSrc,
  contactName,
  contactCompany,
  ctaLabel,
  mountFrame,
  x = 80,
  y = 780,
  width = 920,
  colorMode,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - mountFrame;
  if (local < 0) return null;

  const palette = PALETTES[colorMode];
  const opts = { extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };
  const qrIn = spring({ frame: local, fps, config: { damping: 14, stiffness: 200 } });
  const pillLocal = Math.max(0, local - 30);
  const pillIn = spring({ frame: pillLocal, fps, config: { damping: 16, stiffness: 160 } });
  const showPill = local >= 30;

  return (
    <div style={{ position: "absolute", left: x, top: y, width }}>
      <div
        style={{
          opacity: interpolate(local, [0, 20], [0, 1], opts) * qrIn,
          transform: `translateX(${(1 - qrIn) * -40}px)`,
          marginBottom: 14,
          background: palette.card,
          borderRadius: 20,
          border: `1.5px solid ${palette.line}`,
          boxShadow: `0 8px 32px ${palette.shadow}`,
          padding: "20px 24px",
          display: "flex",
          alignItems: "center",
          gap: 20,
        }}
      >
        <div
          style={{
            width: 120,
            height: 120,
            borderRadius: 12,
            overflow: "hidden",
            background: "#FFFFFF",
            flexShrink: 0,
            border: `1px solid ${palette.line}`,
            padding: 4,
          }}
        >
          <Img src={qrSrc.startsWith("http") ? qrSrc : staticFile(qrSrc)} style={{ width: "100%", height: "100%", objectFit: "contain" }} />
        </div>
        <div style={{ flex: 1 }}>
          <div
            style={{
              fontFamily: labelFont,
              fontSize: 12,
              fontWeight: 600,
              color: palette.inkSoft,
              letterSpacing: 2,
              textTransform: "uppercase",
              marginBottom: 6,
            }}
          >
            Scan to Chat on WhatsApp
          </div>
          <div style={{ fontFamily: headingFont, fontSize: 22, fontWeight: 800, color: palette.ink }}>
            Opens instantly
          </div>
          <div style={{ fontFamily: labelFont, fontSize: 16, fontWeight: 500, color: palette.inkSoft, marginTop: 4 }}>
            {contactName}
            {contactCompany ? ` · ${contactCompany}` : ""}
          </div>
        </div>
      </div>

      {showPill && (
        <div
          style={{
            opacity: pillIn,
            transform: `translateY(${(1 - pillIn) * 20}px)`,
            background: palette.accent,
            borderRadius: 50,
            padding: "30px 0",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <span style={{ fontFamily: headingFont, fontSize: 30, fontWeight: 800, color: "#FFFFFF" }}>
            {ctaLabel}
          </span>
        </div>
      )}
    </div>
  );
};
