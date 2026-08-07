import React from "react";
import type { ColorMode } from "./theme";
import { ZoneHeader } from "./ZoneHeader";
import { QuoteCard } from "./QuoteCard";
import { InfoCard } from "./InfoCard";
import { BudgetRevealSection } from "./BudgetRevealSection";
import { RiskGauge } from "./RiskGauge";
import { CountdownRing } from "./CountdownRing";
import { Calendar } from "./Calendar";
import { AccentPill } from "./AccentPill";
import { StepList } from "./StepList";
import { TopicCard } from "./TopicCard";
import { ComparisonCard } from "./ComparisonCard";
import { RankedListCard } from "./RankedListCard";
import { ChecklistCard } from "./ChecklistCard";
import { LocationPinCard } from "./LocationPinCard";
import { TestimonialCard } from "./TestimonialCard";
import { IconClusterCard } from "./IconClusterCard";
import { ProgressBarCard } from "./ProgressBarCard";
import { ProsConsCard } from "./ProsConsCard";
import { MilestoneTrackCard } from "./MilestoneTrackCard";
import { TrustBadgeCard } from "./TrustBadgeCard";
import { BarChartCard } from "./BarChartCard";
import { MilestoneUnlockCard } from "./MilestoneUnlockCard";
import { CornerCard } from "./CornerCard";

/**
 * A SEPARATE, minimal section -> renderer map used only by the Phase D
 * gallery's isolated preview composition (CardPreview.tsx) — deliberately
 * NOT a shared registry with XiaojinEditorial.tsx's own render tree.
 *
 * XiaojinEditorial.tsx's paint order encodes two confirmed, documented bug
 * fixes (content-after-SpeakerCard; outro-before-qrContact — see that
 * file's own comments), so refactoring its inline JSX into a shared
 * registry was considered and deliberately NOT done here: it would be the
 * single riskiest edit in this whole effort for a feature (gallery tile
 * previews) that doesn't need it. This file only mirrors each card's own
 * prop spread — {...item} plus whichever of colorMode/headingFont/
 * labelFont that section's real render call site actually passes (copied
 * verbatim from XiaojinEditorial.tsx, e.g. `pills` there passes no
 * labelFont either) — so a gallery tile renders pixel-identical to how the
 * same item would render inside a real video.
 */

export interface CommonCardProps {
  colorMode: ColorMode;
  headingFont: string;
  labelFont: string;
}

type CardRenderer = (item: Record<string, unknown>, common: CommonCardProps) => React.ReactNode;

// `{...(item as any)}` below is a deliberate, narrow escape hatch: `item` is
// `Record<string, unknown>` because this ONE registry spans 22 unrelated
// prop shapes — an index-signature type can't statically prove it supplies
// each component's specific required props (title/rows/mountFrame/...),
// even though at runtime it always does (every item here was constructed
// against contracts/render_props.schema.json, the real source of truth for
// this shape). This matches how the rest of the editor already treats card
// items — untyped dicts validated by JSON Schema at runtime, not by
// TypeScript — rather than hand-writing 22 redundant prop interfaces here.
export const CARD_RENDERERS: Record<string, CardRenderer> = {
  zoneHeaders: (item, c) => <ZoneHeader {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  quotes: (item, c) => <QuoteCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  dataCards: (item, c) => <InfoCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  beforeAfter: (item, c) => <BudgetRevealSection {...(item as any)} headingFont={c.headingFont} labelFont={c.labelFont} />,
  gauges: (item, c) => <RiskGauge {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  countdowns: (item, c) => <CountdownRing {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  calendarEvents: (item, c) => <Calendar {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  pills: (item, c) => <AccentPill {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} />,
  stepLists: (item, c) => <StepList {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  topicCards: (item, c) => <TopicCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  comparisons: (item, c) => <ComparisonCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  rankedLists: (item, c) => <RankedListCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  checklists: (item, c) => <ChecklistCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  locationPins: (item, c) => <LocationPinCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  testimonials: (item, c) => <TestimonialCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  iconClusters: (item, c) => <IconClusterCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  progressBars: (item, c) => <ProgressBarCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  prosCons: (item, c) => <ProsConsCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  milestoneTracks: (item, c) => <MilestoneTrackCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  trustBadges: (item, c) => <TrustBadgeCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  barCharts: (item, c) => <BarChartCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  milestoneUnlocks: (item, c) => <MilestoneUnlockCard {...(item as any)} colorMode={c.colorMode} headingFont={c.headingFont} labelFont={c.labelFont} />,
  // CornerCard renders inside SpeakerCard as a positioned child in the real
  // composition (no colorMode/font props there either) — preview it
  // centered on a plain background instead of faking a facecam.
  cornerCards: (item) => <CornerCard {...(item as any)} />,
};

export function hasGalleryRenderer(section: string): boolean {
  return section in CARD_RENDERERS;
}
