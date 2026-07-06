import { BRAND, COLOR, W } from "./theme";

export interface BrandBarProps {
  name: string;
  tagline?: string;
  font?: string;
}

export const BrandBar: React.FC<BrandBarProps> = ({ name, tagline, font = "inherit" }) => (
  <div
    style={{
      position: "absolute",
      left: 0,
      top: BRAND.y,
      width: W,
      height: BRAND.h,
      background: "rgba(242,235,224,0.92)",
      borderTop: `1px solid ${COLOR.line}`,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      gap: 10,
    }}
  >
    <span style={{ fontFamily: font, fontSize: 15, fontWeight: 800, letterSpacing: 2, color: COLOR.ink }}>
      {name}
    </span>
    {tagline ? (
      <>
        <span style={{ width: 4, height: 4, borderRadius: 2, background: COLOR.terracotta }} />
        <span style={{ fontFamily: font, fontSize: 13, fontWeight: 500, letterSpacing: 1, color: COLOR.inkSoft }}>
          {tagline}
        </span>
      </>
    ) : null}
  </div>
);
