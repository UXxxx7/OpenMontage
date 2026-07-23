/**
 * XiaojinEditorial — the real xiaojin-editorial composition, ported from
 * VeLL-lab/video-studio's `motion/vell-renewal-reminder/` reference build
 * and generalized to take content as props instead of per-project
 * generated/*.ts modules.
 *
 * This is distinct from the existing `ReferenceStyleEdit.tsx` composition
 * (used by the `WhatsAppReferenceEdit` id in Root.tsx), which is a loose,
 * generic approximation of the same source video (soft cream backdrop,
 * landscape 16:9, no card/nav/compliance chrome) built independently on
 * this side of the project. This component instead reproduces the actual
 * documented spec — vertical 9:16, floating card (never full-bleed),
 * persistent ChapterNav, karaoke captions, ComplianceBar, rainbow
 * progress bar — so a job that requests `xiaojin-editorial` gets the real
 * style, not a lookalike. Keep both compositions; do not merge them
 * silently, since they encode two different design conclusions.
 *
 * `contentBeats`, `intro`, `outro`, and `brand` are optional — the first
 * version of this file only had SpeakerCard/ChapterNav/Captions/
 * ComplianceBar/RainbowProgressBar (the "chrome"), missing entirely the
 * "graphic content side" the style doc calls its other signature move.
 * These four close that gap with generalized versions of the components
 * that recur across all 4 video-studio motion projects (see each
 * component's own doc comment for what did NOT get ported, and why).
 */
import Ajv2020 from "ajv/dist/2020";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadNotoSansTC } from "@remotion/google-fonts/NotoSansTC";
import { AbsoluteFill, CalculateMetadataFunction } from "remotion";
import renderPropsSchema from "../../contracts/render_props.schema.json";

// headingFont/labelFont defaulted to CSS "inherit" (no font loaded at all)
// and pipeline_runner.py never sets them — every real production render
// was falling back to whatever generic font the render environment
// happened to have, not the reference build's Inter/Noto Sans TC (confirmed:
// zero references to headingFont/labelFont anywhere in pipeline_runner.py
// or content_planner.py). Loading and defaulting to the same fonts
// video-studio's vell-renewal-fresh reference uses — tc (includes latin) for
// headings so both languages render correctly, inter for the small
// UPPERCASE labels/eyebrows, matching how the reference assigns them.
const { fontFamily: _defaultHeadingFont } = loadNotoSansTC("normal", {
  weights: ["500", "700", "800"],
  subsets: ["chinese-traditional", "latin"],
});
const { fontFamily: _defaultLabelFont } = loadInter("normal", {
  weights: ["400", "500", "600", "700", "800"],
  subsets: ["latin"],
});
import { BrandBar } from "./components/xiaojin/BrandBar";
import { BudgetRevealSection, BudgetRevealSectionProps } from "./components/xiaojin/BudgetRevealSection";
import { Calendar, CalendarProps } from "./components/xiaojin/Calendar";
import { Captions, CaptionPhrase } from "./components/xiaojin/Captions";
import { ChapterNav, Chapter } from "./components/xiaojin/ChapterNav";
import { ComplianceBar } from "./components/xiaojin/ComplianceBar";
import { ContentBeat, ContentZone } from "./components/xiaojin/ContentZone";
import { CornerCard, CornerCardProps } from "./components/xiaojin/CornerCard";
import { Section, SectionLayer } from "./components/xiaojin/SectionLayer";
import { QuoteCard, QuoteCardProps } from "./components/xiaojin/QuoteCard";
import { CountdownRing, CountdownRingProps } from "./components/xiaojin/CountdownRing";
import { InfoCard, InfoCardProps } from "./components/xiaojin/InfoCard";
import { IntroTitle } from "./components/xiaojin/IntroTitle";
import { StatsHookIntro } from "./components/xiaojin/StatsHookIntro";
import { TitleImpactIntro } from "./components/xiaojin/TitleImpactIntro";
import { ChipsIntro } from "./components/xiaojin/ChipsIntro";
import { OutroSection } from "./components/xiaojin/OutroSection";
import { AccentPill, AccentPillProps } from "./components/xiaojin/AccentPill";
import { QRContactCard, QRContactCardProps } from "./components/xiaojin/QRContactCard";
import { RainbowProgressBar } from "./components/xiaojin/RainbowProgressBar";
import { RiskGauge, RiskGaugeProps } from "./components/xiaojin/RiskGauge";
import { SpeakerCard, SpeakerCardOpacityKeyframe, SpeakerCardScene } from "./components/xiaojin/SpeakerCard";
import { StepList, StepListProps } from "./components/xiaojin/StepList";
import { TopicCard, TopicCardProps } from "./components/xiaojin/TopicCard";
import { ZoneHeader, ZoneHeaderProps } from "./components/xiaojin/ZoneHeader";
import { Presenter, PresenterProps } from "./components/xiaojin/Presenter";
import { ComparisonCard, ComparisonCardProps } from "./components/xiaojin/ComparisonCard";
import { RankedListCard, RankedListCardProps } from "./components/xiaojin/RankedListCard";
import { ChecklistCard, ChecklistCardProps } from "./components/xiaojin/ChecklistCard";
import { LocationPinCard, LocationPinCardProps } from "./components/xiaojin/LocationPinCard";
import { TestimonialCard, TestimonialCardProps } from "./components/xiaojin/TestimonialCard";
import { IconClusterCard, IconClusterCardProps } from "./components/xiaojin/IconClusterCard";
import { ProgressBarCard, ProgressBarCardProps } from "./components/xiaojin/ProgressBarCard";
import { ProsConsCard, ProsConsCardProps } from "./components/xiaojin/ProsConsCard";
import { MilestoneTrackCard, MilestoneTrackCardProps } from "./components/xiaojin/MilestoneTrackCard";
import { TrustBadgeCard, TrustBadgeCardProps } from "./components/xiaojin/TrustBadgeCard";
import { BarChartCard, BarChartCardProps } from "./components/xiaojin/BarChartCard";
import { MilestoneUnlockCard, MilestoneUnlockCardProps } from "./components/xiaojin/MilestoneUnlockCard";
import { ColorMode } from "./components/xiaojin/theme";

export interface ComplianceInfo {
  agentNameZh: string;
  agentNameEn: string;
  titleZh: string;
  licenseNo: string;
  insurer: string;
}

export interface BrandInfo {
  company: string;
  label: string;
}

export interface IntroInfo {
  eyebrow: string;
  title: string;
  subtitle: string;
  /**
   * Which of the 4 video-studio CLAUDE-xiaojin-editorial.md intro patterns to
   * render. Omit (or "title_card") for the original, unchanged behavior —
   * IntroTitle, a dark scrim over the speaker footage. The other 3 variants
   * were previously unported; added 2026-07-16 for visual variety across
   * jobs so every video doesn't open the same way regardless of content tone.
   */
  variant?: "title_card" | "stats_hook" | "title_impact" | "chips";
  /** Only used by variant "title_impact" — top-right brand chip. Omit to skip it. */
  brandLabel?: string;
}

/** Matches contract②'s dataCards item shape exactly — colorMode/fonts come from the parent. */
export type DataCard = Omit<InfoCardProps, "colorMode" | "headingFont" | "labelFont">;
export type Gauge = Omit<RiskGaugeProps, "colorMode" | "headingFont" | "labelFont">;
export type BeforeAfter = Omit<BudgetRevealSectionProps, "headingFont" | "labelFont">;
export type Countdown = Omit<CountdownRingProps, "colorMode" | "headingFont" | "labelFont">;
export type CalendarEvent = Omit<CalendarProps, "colorMode" | "headingFont" | "labelFont">;
export type Pill = Omit<AccentPillProps, "colorMode" | "headingFont">;
export type QRContact = Omit<QRContactCardProps, "colorMode" | "headingFont" | "labelFont">;
export type Quote = Omit<QuoteCardProps, "colorMode" | "headingFont" | "labelFont">;
export type ZoneHeaderItem = Omit<ZoneHeaderProps, "colorMode" | "headingFont" | "labelFont">;
export type StepListItem = Omit<StepListProps, "colorMode" | "headingFont" | "labelFont">;
export type TopicCardItem = Omit<TopicCardProps, "colorMode" | "headingFont" | "labelFont">;
export type CornerCardItem = CornerCardProps;
// "CardItem" (not "Item") suffix deliberately, matching CornerCardItem — these
// are the outer per-card props for the props array, and Ranked/Checklist/
// IconCluster each already export their OWN per-row "...Item" type from their
// own component file (RankedListItem/ChecklistItem/IconClusterItem); reusing
// that exact name here for the outer card type would shadow-confuse the two.
export type ComparisonCardItem = Omit<ComparisonCardProps, "colorMode" | "headingFont" | "labelFont">;
export type RankedListCardItem = Omit<RankedListCardProps, "colorMode" | "headingFont" | "labelFont">;
export type ChecklistCardItem = Omit<ChecklistCardProps, "colorMode" | "headingFont" | "labelFont">;
export type LocationPinCardItem = Omit<LocationPinCardProps, "colorMode" | "headingFont" | "labelFont">;
export type TestimonialCardItem = Omit<TestimonialCardProps, "colorMode" | "headingFont" | "labelFont">;
export type IconClusterCardItem = Omit<IconClusterCardProps, "colorMode" | "headingFont" | "labelFont">;
export type ProgressBarCardItem = Omit<ProgressBarCardProps, "colorMode" | "headingFont" | "labelFont">;
export type ProsConsCardItem = Omit<ProsConsCardProps, "colorMode" | "headingFont" | "labelFont">;
export type MilestoneTrackCardItem = Omit<MilestoneTrackCardProps, "colorMode" | "headingFont" | "labelFont">;
export type TrustBadgeCardItem = Omit<TrustBadgeCardProps, "colorMode" | "headingFont" | "labelFont">;
export type BarChartCardItem = Omit<BarChartCardProps, "colorMode" | "headingFont" | "labelFont">;
export type MilestoneUnlockCardItem = Omit<MilestoneUnlockCardProps, "colorMode" | "headingFont" | "labelFont">;

export interface OutroInfo {
  kicker: string;
  headline: string;
  headlineAccent?: string;
  subtext: string;
  ctaLabel: string;
  footerLabel: string;
  /** Frame the outro takes over the content zone. Keep contentBeats ending before this. */
  fromFrame: number;
}

export interface XiaojinEditorialProps extends Record<string, unknown> {
  videoSrc: string;
  /** Drives calculateXiaojinEditorialMetadata's durationInFrames — must match videoSrc's real length. */
  durationSeconds: number;
  colorMode: ColorMode;
  /** Calibrated per source video — see SpeakerCard's doc comment. Required, no safe default. */
  speakerObjectPosition: string;
  scenes: SpeakerCardScene[];
  opacityKeyframes?: SpeakerCardOpacityKeyframe[];
  chapters: Chapter[];
  introOutFrame: number;
  captions: CaptionPhrase[];
  /** The graphic content side opposite the speaker card. Omit for a chrome-only build. */
  contentBeats?: ContentBeat[];
  /** Full-canvas chapter takeovers (background + header + icon) — see SectionLayer. */
  sections?: Section[];
  /** Pull-quote typography moments (data-less videos' canvas motion). */
  quotes?: Quote[];
  /** Count-up stat cards (contract② "[P3 NEW capability]"). Renders alongside contentBeats, not in place of it. */
  dataCards?: DataCard[];
  /** Dramatic two-value before/after reveals (e.g. a cost/metric that jumped over time). */
  beforeAfter?: BeforeAfter[];
  /** Semicircle risk/status gauges — Data Display Analysis "risk consequence" rows. */
  gauges?: Gauge[];
  /** Circular countdown rings — Data Display Analysis "countdown days" rows. */
  countdowns?: Countdown[];
  /** Mini-calendars with a pulsing target-date marker — Data Display Analysis "specific date" rows. */
  calendarEvents?: CalendarEvent[];
  /** Full-width terracotta takeaway pills stacked under their primary graphics (see AccentPill). */
  pills?: Pill[];
  /** Compact left-aligned section headers for normal (non-takeover) chapters — see ZoneHeader. */
  zoneHeaders?: ZoneHeaderItem[];
  /** Ghosted numbered step skeletons, activating one row per spoken beat — see StepList. */
  stepLists?: StepListItem[];
  /** Icon + statement cards for supporting lines with no hard data — see TopicCard. */
  topicCards?: TopicCardItem[];
  /** Compact illustration overlays anchored inside the SpeakerCard — see CornerCard. */
  cornerCards?: CornerCardItem[];
  /** Side-by-side 2-3 column comparisons across multiple attributes — see ComparisonCard. */
  comparisons?: ComparisonCardItem[];
  /** Several numbers compared/ranked against each other in one beat — see RankedListCard. */
  rankedLists?: RankedListCardItem[];
  /** Items ticking on one by one, each on its own spoken beat — see ChecklistCard. */
  checklists?: ChecklistCardItem[];
  /** A named place, map-pin drop — see LocationPinCard. */
  locationPins?: LocationPinCardItem[];
  /** A third party's quoted words — see TestimonialCard. */
  testimonials?: TestimonialCardItem[];
  /** An unordered set of related named things — see IconClusterCard. */
  iconClusters?: IconClusterCardItem[];
  /** Straight linear completion bars — see ProgressBarCard. */
  progressBars?: ProgressBarCardItem[];
  /** Polarized two-column pros/cons — see ProsConsCard. */
  prosCons?: ProsConsCardItem[];
  /** Lightweight inline history dot-tracks — see MilestoneTrackCard. */
  milestoneTracks?: MilestoneTrackCardItem[];
  /** Credential/authority stacks — see TrustBadgeCard. */
  trustBadges?: TrustBadgeCardItem[];
  /** Real axis-based column charts — see BarChartCard. */
  barCharts?: BarChartCardItem[];
  /** Celebratory single-number reveals — see MilestoneUnlockCard. */
  milestoneUnlocks?: MilestoneUnlockCardItem[];
  /** QR + WhatsApp CTA close. Only set when a real contact URL was actually supplied. */
  qrContact?: QRContact;
  /** "Pattern 2" dark title-card intro (see IntroTitle's doc comment). Omit to skip. */
  intro?: IntroInfo;
  /** Takes over the content zone from `fromFrame` onward. Omit to skip. */
  outro?: OutroInfo;
  /**
   * Exactly one of `compliance` / `brand`, or neither. `compliance` is for
   * regulated content requiring a persistent disclosure strip; `brand` is
   * the lighter non-regulatory equivalent. Passing both is a caller error —
   * this component renders compliance first if both are given, but don't
   * rely on that; pick one.
   */
  compliance?: ComplianceInfo;
  brand?: BrandInfo;
  /** presenter mode: speaker inset in the lower zone during b-roll windows. */
  presenter?: PresenterProps;
  headingFont?: string;
  labelFont?: string;
}

// No CI currently validates props against contracts/render_props.schema.json anywhere
// in the repo, and several xiaojin components (OutroSection/ComplianceBar/IntroTitle)
// declare their sub-fields as required strings with no runtime guards — a partially
// filled optional object (e.g. outro with only fromFrame) won't crash, it'll just
// silently render blank/`undefined` text. Rather than scatter defensive guards through
// every component, validate once here, before any frame renders, so malformed props
// fail loudly instead of rendering garbage.
const ajv = new Ajv2020({ allErrors: true, strict: false });
const validateRenderProps = ajv.compile(renderPropsSchema);

// durationSeconds (contract②, required) drives frame count directly — no probing the
// video file, unlike ReferenceStyleEdit's calculateMetadata (that composition is slated
// for retirement; don't anchor new code to its helper). Ported from postxhs's
// calculatePostXhsEditorialMetadata, which solves the same contract-driven-duration problem.
export const calculateXiaojinEditorialMetadata: CalculateMetadataFunction<
  XiaojinEditorialProps
> = async ({ props }) => {
  if (!validateRenderProps(props)) {
    throw new Error(
      `XiaojinEditorial props failed contract② validation:\n${ajv.errorsText(
        validateRenderProps.errors,
        { separator: "\n" }
      )}`
    );
  }

  const fps = 30;
  return {
    durationInFrames: Math.max(1, Math.ceil((props.durationSeconds || 1) * fps)),
    fps,
    width: 1080,
    height: 1920,
  };
};

export const XiaojinEditorial: React.FC<XiaojinEditorialProps> = ({
  videoSrc,
  colorMode,
  speakerObjectPosition,
  scenes,
  opacityKeyframes,
  chapters,
  introOutFrame,
  captions,
  contentBeats,
  sections,
  quotes,
  pills,
  dataCards,
  beforeAfter,
  gauges,
  countdowns,
  calendarEvents,
  zoneHeaders,
  stepLists,
  topicCards,
  cornerCards,
  comparisons,
  rankedLists,
  checklists,
  locationPins,
  testimonials,
  iconClusters,
  progressBars,
  prosCons,
  milestoneTracks,
  trustBadges,
  barCharts,
  milestoneUnlocks,
  qrContact,
  intro,
  outro,
  compliance,
  brand,
  presenter,
  headingFont = _defaultHeadingFont,
  labelFont = _defaultLabelFont,
}) => {
  const bg = colorMode === "warm" ? "#F2EBE0" : "#0D1117";

  return (
    <AbsoluteFill style={{ background: bg }}>
      {/* Full-canvas section takeovers render FIRST — they are backgrounds;
          the card, graphics and chrome all sit above them. */}
      {sections?.length ? (
        <SectionLayer
          sections={sections}
          baseColorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ) : null}
      <SpeakerCard
        videoSrc={videoSrc}
        scenes={scenes}
        opacityKeyframes={opacityKeyframes}
        objectPosition={speakerObjectPosition}
        colorMode={colorMode}
      >
        {cornerCards?.map((card, i) => (
          <CornerCard key={i} {...card} />
        ))}
      </SpeakerCard>
      {/*
        contentBeats/dataCards/outro all render AFTER SpeakerCard (not
        before) so none of them are ever silently hidden behind it — confirmed
        as a real bug via render test on two separate components: the
        canonical fixture's data card (x:80,y:900) and a test outro
        (fromFrame-gated, full CTA takeover) were both fully invisible when
        painted before an opaque, Dominant-mode SpeakerCard (960x1100 at
        y:104 → spans to y:1204, covering nearly all of either component's
        content). Content-zone elements only make visual sense once the card
        has shrunk out of the way (Workflow mode) or the outro has taken over
        — painting them on top guarantees they're visible regardless of
        whether the caller's scene schedule actually shrinks the card first.
      */}
      {contentBeats ? <ContentZone beats={contentBeats} /> : null}
      {zoneHeaders?.map((header, i) => (
        <ZoneHeader key={i} {...header} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {quotes?.map((q, i) => (
        <QuoteCard key={`q${i}`} {...q} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {dataCards?.map((card, i) => (
        <InfoCard
          key={i}
          {...card}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ))}
      {beforeAfter?.map((reveal, i) => (
        <BudgetRevealSection
          key={i}
          {...reveal}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ))}
      {gauges?.map((gauge, i) => (
        <RiskGauge
          key={i}
          {...gauge}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ))}
      {countdowns?.map((countdown, i) => (
        <CountdownRing
          key={i}
          {...countdown}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ))}
      {calendarEvents?.map((event, i) => (
        <Calendar
          key={i}
          {...event}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ))}
      {pills?.map((pill, i) => (
        <AccentPill
          key={i}
          {...pill}
          colorMode={colorMode}
          headingFont={headingFont}
        />
      ))}
      {stepLists?.map((list, i) => (
        <StepList key={i} {...list} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {topicCards?.map((card, i) => (
        <TopicCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {comparisons?.map((card, i) => (
        <ComparisonCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {rankedLists?.map((card, i) => (
        <RankedListCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {checklists?.map((card, i) => (
        <ChecklistCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {locationPins?.map((card, i) => (
        <LocationPinCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {testimonials?.map((card, i) => (
        <TestimonialCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {iconClusters?.map((card, i) => (
        <IconClusterCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {progressBars?.map((card, i) => (
        <ProgressBarCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {prosCons?.map((card, i) => (
        <ProsConsCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {milestoneTracks?.map((card, i) => (
        <MilestoneTrackCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {trustBadges?.map((card, i) => (
        <TrustBadgeCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {barCharts?.map((card, i) => (
        <BarChartCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {milestoneUnlocks?.map((card, i) => (
        <MilestoneUnlockCard key={i} {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
      ))}
      {/* outro renders BEFORE qrContact (not the other way around): OutroSection
          paints an opaque full-canvas background (y=88 to H-72). Rendering
          qrContact first meant it silently sat UNDERNEATH that background
          whenever both were populated for the same job — the QR card was
          fully computed and mounted, just permanently hidden. pipeline_runner
          also now anchors qrContact's mountFrame/y off outro's own frame and
          footer position when outro is present (see _build_apply_style_props),
          so the two read as one continuous end-card moment instead of two
          independently-timed overlays that happen to occupy the same span. */}
      {outro ? (
        <OutroSection
          kicker={outro.kicker}
          headline={outro.headline}
          headlineAccent={outro.headlineAccent}
          subtext={outro.subtext}
          ctaLabel={outro.ctaLabel}
          footerLabel={outro.footerLabel}
          outroFromFrame={outro.fromFrame}
          colorMode={colorMode}
          font={headingFont}
        />
      ) : null}
      {qrContact ? (
        <QRContactCard
          {...qrContact}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ) : null}
      {intro && intro.variant === "stats_hook" ? (
        <StatsHookIntro
          eyebrow={intro.eyebrow}
          title={intro.title}
          subtitle={intro.subtitle}
          introOutFrame={introOutFrame}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ) : intro && intro.variant === "title_impact" ? (
        <TitleImpactIntro
          eyebrow={intro.eyebrow}
          title={intro.title}
          subtitle={intro.subtitle}
          brandLabel={intro.brandLabel}
          introOutFrame={introOutFrame}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ) : intro && intro.variant === "chips" ? (
        <ChipsIntro
          chapters={chapters}
          introOutFrame={introOutFrame}
          colorMode={colorMode}
          labelFont={labelFont}
        />
      ) : intro ? (
        <IntroTitle
          eyebrow={intro.eyebrow}
          title={intro.title}
          subtitle={intro.subtitle}
          introOutFrame={introOutFrame}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ) : null}
      {presenter ? <Presenter {...presenter} colorMode={colorMode} /> : null}
      <ChapterNav
        chapters={chapters}
        introOutFrame={introOutFrame}
        colorMode={colorMode}
        headingFont={headingFont}
        labelFont={labelFont}
      />
      <Captions
        captions={captions}
        introOutFrame={introOutFrame}
        colorMode={colorMode}
        font={headingFont}
      />
      {compliance ? (
        <ComplianceBar {...compliance} font={headingFont} />
      ) : brand ? (
        <BrandBar {...brand} colorMode={colorMode} font={headingFont} />
      ) : null}
      <RainbowProgressBar />
    </AbsoluteFill>
  );
};
