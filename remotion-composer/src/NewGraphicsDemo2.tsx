/**
 * Quick illustration composition for round 3 of the new xiaojin-editorial
 * graphic types (ProgressBar / ProsCons / MilestoneTrack / TrustBadge /
 * BarChart / MilestoneUnlock) — NOT a real video, just a fast standalone
 * preview. Kept separate from NewGraphicsDemo.tsx (round 2) rather than one
 * combined reel, so each batch has its own short, focused demo.
 */
import { AbsoluteFill } from "remotion";
import { PALETTES } from "./components/xiaojin/theme";
import { ProgressBarCard } from "./components/xiaojin/ProgressBarCard";
import { ProsConsCard } from "./components/xiaojin/ProsConsCard";
import { MilestoneTrackCard } from "./components/xiaojin/MilestoneTrackCard";
import { TrustBadgeCard } from "./components/xiaojin/TrustBadgeCard";
import { BarChartCard } from "./components/xiaojin/BarChartCard";
import { MilestoneUnlockCard } from "./components/xiaojin/MilestoneUnlockCard";

const SEG = 90; // frames per segment

export const NewGraphicsDemo2: React.FC = () => {
  const palette = PALETTES.warm;
  return (
    <AbsoluteFill style={{ background: palette.bg }}>
      <ProgressBarCard
        colorMode="warm"
        mountFrame={0 * SEG}
        endFrame={0 * SEG + 85}
        y={900}
        title="RENEWAL STEPS"
        label="Document review"
        percent={80}
        subtext="4 of 5 steps complete"
      />
      <ProsConsCard
        colorMode="warm"
        mountFrame={1 * SEG}
        endFrame={1 * SEG + 85}
        y={760}
        title="Renew Now vs. Let It Lapse"
        prosLabel="RENEW"
        consLabel="LAPSE"
        pros={["Same rate locked in", "Coverage stays active", "No new medical exam"]}
        cons={["Rates may increase", "New underwriting required", "Gap in coverage"]}
      />
      <MilestoneTrackCard
        colorMode="warm"
        mountFrame={2 * SEG}
        endFrame={2 * SEG + 85}
        y={860}
        title="Policy Timeline"
        milestones={[
          { label: "Purchased", sublabel: "2023" },
          { label: "Claim Filed", sublabel: "2024" },
          { label: "Renewal Due", sublabel: "2026" },
        ]}
      />
      <TrustBadgeCard
        colorMode="warm"
        mountFrame={3 * SEG}
        endFrame={3 * SEG + 85}
        y={840}
        title="Credentials"
        badges={[
          { icon: "shield", primary: "Licensed Agent", secondary: "CA LICENSE #88291" },
          { icon: "star", primary: "8 Years", secondary: "SERVING BAY AREA FAMILIES" },
        ]}
      />
      <BarChartCard
        colorMode="warm"
        mountFrame={4 * SEG}
        endFrame={4 * SEG + 85}
        y={820}
        title="Avg. Claim Payout by Plan"
        items={[
          { label: "BASIC", value: 5000, displayValue: "$5K" },
          { label: "STANDARD", value: 15000, displayValue: "$15K" },
          { label: "PREMIUM", value: 40000, displayValue: "$40K" },
        ]}
      />
      <MilestoneUnlockCard
        colorMode="warm"
        mountFrame={5 * SEG}
        endFrame={5 * SEG + 85}
        y={780}
        value={1000}
        suffix="+"
        label="Families Protected"
        icon="award"
      />
    </AbsoluteFill>
  );
};
