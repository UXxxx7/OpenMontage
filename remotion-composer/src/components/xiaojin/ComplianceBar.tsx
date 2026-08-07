/**
 * Persistent regulatory disclosure strip — required on every frame for
 * regulated content (e.g. HK insurance). Ported from video-studio's
 * ComplianceBar.tsx; generalized to take the fact-sheet fields as props
 * instead of importing a per-project generated/compliance.ts module.
 *
 * This maps directly to the fact_* form fields already prototyped in
 * video-studio's own `service/app.py` (/jobs endpoint) — reuse that field
 * set when wiring a real intake form for this composition.
 */
import { AbsoluteFill } from "remotion";
import { ColorMode, COMPLIANCE, PALETTES, W } from "./theme";

export interface ComplianceBarProps {
  agentNameZh: string;
  agentNameEn: string;
  titleZh: string;
  licenseNo: string;
  insurer: string;
  colorMode?: ColorMode;
  font?: string;
  /** Editor-only position override — omit for the default pinned-bottom,
   *  full-width placement (COMPLIANCE.y / W from theme.ts). */
  x?: number;
  y?: number;
  width?: number;
}

export const ComplianceBar: React.FC<ComplianceBarProps> = ({
  agentNameZh,
  agentNameEn,
  titleZh,
  licenseNo,
  insurer,
  font = "inherit",
  x = 0,
  y = COMPLIANCE.y,
  width = W,
}) => {
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div
        style={{
          position: "absolute",
          left: x,
          top: y,
          width,
          height: COMPLIANCE.h,
          background: "rgba(13,17,23,0.94)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 16,
          paddingInline: 32,
        }}
      >
        <span style={{ fontFamily: font, color: "#FFF", fontSize: 26, fontWeight: 700 }}>
          {agentNameZh} {agentNameEn}
        </span>
        <Dot />
        <span style={{ fontFamily: font, color: "rgba(255,255,255,0.78)", fontSize: 22 }}>
          {titleZh}
        </span>
        <Dot />
        <span
          style={{
            fontFamily: font,
            color: "rgba(255,255,255,0.78)",
            fontSize: 20,
            letterSpacing: 0.5,
          }}
        >
          {licenseNo}
        </span>
        <Dot />
        <span style={{ fontFamily: font, color: "rgba(255,255,255,0.78)", fontSize: 22 }}>
          {insurer}
        </span>
      </div>
    </AbsoluteFill>
  );
};

const Dot: React.FC = () => (
  <span style={{ color: PALETTES.warm.accent, fontSize: 16, opacity: 0.9 }}>•</span>
);
