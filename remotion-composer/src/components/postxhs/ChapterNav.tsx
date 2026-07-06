import { useCurrentFrame } from "remotion";
import { COLOR, NAV, W } from "./theme";

export interface Chapter {
  at: number;
  label: string;
}

export interface ChapterNavProps {
  chapters: Chapter[];
  font?: string;
}

export const ChapterNav: React.FC<ChapterNavProps> = ({ chapters, font = "inherit" }) => {
  const frame = useCurrentFrame();
  if (chapters.length === 0) return null;

  let active = 0;
  chapters.forEach((c, i) => {
    if (frame >= c.at) active = i;
  });

  return (
    <div
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        width: W,
        height: NAV.h,
        background: "rgba(242,235,224,0.96)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 32,
      }}
    >
      {chapters.map((c, i) => {
        const on = i === active;
        return (
          <div key={c.label} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 3 }}>
            <span
              style={{
                fontFamily: font,
                fontSize: 13,
                letterSpacing: 1.5,
                textTransform: "uppercase",
                fontWeight: on ? 700 : 500,
                color: on ? COLOR.terracotta : COLOR.inkSoft,
              }}
            >
              {c.label}
            </span>
            {on && <div style={{ width: 5, height: 5, borderRadius: 3, background: COLOR.terracotta }} />}
          </div>
        );
      })}
    </div>
  );
};
