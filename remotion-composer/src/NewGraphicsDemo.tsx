/**
 * Quick illustration composition for the new xiaojin-editorial graphic
 * types — NOT a real video, just a fast standalone preview so the new
 * components can be eyeballed before/after wiring them into
 * content_planner.py's LLM vocabulary. Round 2 (Comparison/RankedList/
 * Checklist/LocationPin/Testimonial/IconCluster) + round 3 (ProgressBar/
 * ProsCons/MilestoneTrack/TrustBadge/BarChart/MilestoneUnlock), 12 segments.
 */
import { AbsoluteFill } from "remotion";
import { PALETTES } from "./components/xiaojin/theme";
import { ComparisonCard } from "./components/xiaojin/ComparisonCard";
import { RankedListCard } from "./components/xiaojin/RankedListCard";
import { ChecklistCard } from "./components/xiaojin/ChecklistCard";
import { LocationPinCard } from "./components/xiaojin/LocationPinCard";
import { TestimonialCard } from "./components/xiaojin/TestimonialCard";
import { IconClusterCard } from "./components/xiaojin/IconClusterCard";
import { ProgressBarCard } from "./components/xiaojin/ProgressBarCard";
import { ProsConsCard } from "./components/xiaojin/ProsConsCard";
import { MilestoneTrackCard } from "./components/xiaojin/MilestoneTrackCard";
import { TrustBadgeCard } from "./components/xiaojin/TrustBadgeCard";
import { BarChartCard } from "./components/xiaojin/BarChartCard";
import { MilestoneUnlockCard } from "./components/xiaojin/MilestoneUnlockCard";

const SEG = 90; // frames per segment

export const NewGraphicsDemo: React.FC = () => {
  const palette = PALETTES.warm;
  return (
    <AbsoluteFill style={{ background: palette.bg }}>
      <ComparisonCard
        colorMode="warm"
        mountFrame={0 * SEG}
        endFrame={0 * SEG + 85}
        y={760}
        title="旧方案 VS 新方案"
        columns={[
          {
            label: "旧方案",
            labelEn: "BEFORE",
            accent: "bad",
            items: ["人工逐条剪辑", "只有日历/时间线/弹窗卡片", "素材来源单一"],
          },
          {
            label: "新方案",
            labelEn: "AFTER",
            accent: "good",
            items: ["AI 自动规划视觉", "动画库持续扩充", "多素材智能合成"],
          },
        ]}
      />
      <RankedListCard
        colorMode="warm"
        mountFrame={1 * SEG}
        endFrame={1 * SEG + 85}
        y={760}
        title="本周素材使用率"
        items={[
          { label: "字幕高亮", labelEn: "CAPTIONS", value: 94, suffix: "%" },
          { label: "品牌模板", labelEn: "BRAND STYLE", value: 81, suffix: "%" },
          { label: "B-ROLL 合成", labelEn: "B-ROLL", value: 63, suffix: "%" },
        ]}
      />
      <ChecklistCard
        colorMode="warm"
        mountFrame={2 * SEG}
        endFrame={2 * SEG + 85}
        y={760}
        title="上线前检查"
        items={[
          { label: "字幕已生成", labelEn: "CAPTIONS READY", activateOffset: 0 },
          { label: "品牌样式已渲染", labelEn: "BRAND STYLE RENDERED", activateOffset: 22 },
          { label: "音频已增强", labelEn: "AUDIO ENHANCED", activateOffset: 44 },
        ]}
      />
      <LocationPinCard
        colorMode="warm"
        mountFrame={3 * SEG}
        endFrame={3 * SEG + 85}
        y={860}
        place="香港"
        placeEn="HONG KONG"
        sub="服务覆盖范围已扩展至此"
      />
      <TestimonialCard
        colorMode="warm"
        mountFrame={4 * SEG}
        endFrame={4 * SEG + 85}
        y={820}
        quote="剪辑速度快了不止一倍，客户第一次看到成片就直接通过了。"
        name="David Chan"
        role="Pacific Life · 保险顾问"
      />
      <IconClusterCard
        colorMode="warm"
        mountFrame={5 * SEG}
        endFrame={5 * SEG + 85}
        y={860}
        title="支持的素材来源"
        items={[
          { icon: "chat", label: "WhatsApp", labelEn: "CHAT" },
          { icon: "camera", label: "手机拍摄", labelEn: "CAMERA" },
          { icon: "play", label: "屏幕录制", labelEn: "SCREEN" },
          { icon: "star", label: "AI 生成", labelEn: "AI" },
        ]}
      />
      <ProgressBarCard
        colorMode="warm"
        mountFrame={6 * SEG}
        endFrame={6 * SEG + 85}
        y={900}
        title="RENEWAL STEPS"
        label="Document review"
        percent={80}
        subtext="4 of 5 steps complete"
      />
      <ProsConsCard
        colorMode="warm"
        mountFrame={7 * SEG}
        endFrame={7 * SEG + 85}
        y={760}
        title="Renew Now vs. Let It Lapse"
        prosLabel="RENEW"
        consLabel="LAPSE"
        pros={["Same rate locked in", "Coverage stays active", "No new medical exam"]}
        cons={["Rates may increase", "New underwriting required", "Gap in coverage"]}
      />
      <MilestoneTrackCard
        colorMode="warm"
        mountFrame={8 * SEG}
        endFrame={8 * SEG + 85}
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
        mountFrame={9 * SEG}
        endFrame={9 * SEG + 85}
        y={840}
        title="Credentials"
        badges={[
          { icon: "shield", primary: "Licensed Agent", secondary: "CA LICENSE #88291" },
          { icon: "star", primary: "8 Years", secondary: "SERVING BAY AREA FAMILIES" },
        ]}
      />
      <BarChartCard
        colorMode="warm"
        mountFrame={10 * SEG}
        endFrame={10 * SEG + 85}
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
        mountFrame={11 * SEG}
        endFrame={11 * SEG + 85}
        y={780}
        value={1000}
        suffix="+"
        label="Families Protected"
        icon="award"
      />
    </AbsoluteFill>
  );
};
