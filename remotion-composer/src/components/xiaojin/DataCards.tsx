/**
 * DataCards — contract ② `dataCards` consumer (InfoCard-equivalent).
 *
 * [P2 LOCAL VALIDATION COPY — P3 owns this file's final form.]
 * contracts/render_props.schema.json marks dataCards as "[P3 NEW capability]:
 * P3 must add an InfoCard-equivalent to the xiaojin component set". This is
 * that component, built to the compose-director.md InfoCards spec so P2 can
 * validate the full transcribe→plan→props→render chain locally before P3's
 * branch lands. Spec followed:
 *   - dark ink panel rgba(13,17,23,0.86), radius 16, pill rows
 *   - spring({damping:14, stiffness:220}), translateX(-40→0), row stagger
 *   - count-up values (codex: "If a number is spoken, count it up")
 *   - tone colors: accent #C4714A / good #4F9D69 / bad #CF5448 / normal #FFF
 */
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export interface DataCardRow {
  label: string;
  value: number;
  tone?: "accent" | "good" | "bad" | "normal";
  mountOffset?: number;
  prefix?: string;
  divideBy?: number;
  decimals?: number;
  unit?: string;
}

export interface DataCard {
  title: string;
  x?: number;
  y?: number;
  width?: number;
  mountFrame: number;
  rows: DataCardRow[];
}

const TONE: Record<string, string> = {
  accent: "#C4714A",
  good: "#4F9D69",
  bad: "#CF5448",
  normal: "#FFFFFF",
};

const COUNT_UP_FRAMES = 40;

const formatValue = (row: DataCardRow, progress: number): string => {
  const target = row.value / (row.divideBy && row.divideBy !== 0 ? row.divideBy : 1);
  const current = target * progress;
  const decimals = row.decimals ?? 0;
  // Round instead of floor so the final frame shows the exact target.
  const shown = current.toFixed(decimals);
  return `${row.prefix ?? ""}${Number(shown).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
};

const Row: React.FC<{ row: DataCardRow; cardMount: number; font: string }> = ({ row, cardMount, font }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const mountAt = cardMount + (row.mountOffset ?? 0);
  const age = frame - mountAt;
  if (age < 0) return null;

  const enter = spring({ frame: age, fps, config: { damping: 14, stiffness: 220 } });
  const countProgress = interpolate(age, [0, COUNT_UP_FRAMES], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const tone = TONE[row.tone ?? "normal"] ?? TONE.normal;

  return (
    <div
      style={{
        background: "rgba(255,255,255,0.06)",
        borderRadius: 10,
        padding: "12px 16px",
        marginBottom: 8,
        display: "flex",
        justifyContent: "space-between",
        alignItems: "baseline",
        opacity: enter,
        transform: `translateX(${(1 - enter) * -40}px)`,
      }}
    >
      <span style={{ fontFamily: font, fontSize: 20, fontWeight: 700, color: "#FFFFFF", letterSpacing: 1 }}>
        {row.label}
      </span>
      <span style={{ fontFamily: font, fontSize: 40, fontWeight: 800, color: tone }}>
        {formatValue(row, countProgress)}
        {row.unit ? (
          <span style={{ fontSize: 16, fontWeight: 600, opacity: 0.7, marginLeft: 4 }}>{row.unit}</span>
        ) : null}
      </span>
    </div>
  );
};

export const DataCards: React.FC<{ cards: DataCard[]; font?: string }> = ({ cards, font = "inherit" }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  return (
    <>
      {cards.map((card, i) => {
        const age = frame - card.mountFrame;
        if (age < 0) return null;
        const enter = spring({ frame: age, fps, config: { damping: 14, stiffness: 220 } });
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: card.x ?? 80,
              top: card.y ?? 1100,
              width: card.width ?? 920,
              background: "rgba(13,17,23,0.86)",
              borderRadius: 16,
              padding: "20px 24px",
              boxShadow: "0 8px 32px rgba(0,0,0,0.32)",
              opacity: enter,
              transform: `translateY(${(1 - enter) * 40}px)`,
            }}
          >
            <div
              style={{
                fontFamily: font,
                fontSize: 15,
                fontWeight: 700,
                letterSpacing: 3,
                textTransform: "uppercase",
                color: "rgba(255,255,255,0.55)",
                marginBottom: 14,
              }}
            >
              {card.title}
            </div>
            {card.rows.map((row, j) => (
              <Row key={j} row={row} cardMount={card.mountFrame} font={font} />
            ))}
          </div>
        );
      })}
    </>
  );
};
