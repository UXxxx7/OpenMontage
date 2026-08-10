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
import { useMemo } from "react";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadNotoSansTC } from "@remotion/google-fonts/NotoSansTC";
import { AbsoluteFill, CalculateMetadataFunction, getRemotionEnvironment } from "remotion";
import { computeOutputDuration, mapPropsForCuts, normalizeCuts } from "./cuts";
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
//
// getInfo().fontFamily for NotoSansTC is always literally "Noto Sans TC"
// regardless of which weights/subsets get loaded, so the default prop
// value doesn't need the loadFont() call itself — that side effect (which
// registers the actual font-face glyphs) is triggered separately below,
// inside the component, not at module scope.
const _defaultHeadingFont = "Noto Sans TC";
const _defaultLabelFont = "Inter";
import { BrandBar } from "./components/xiaojin/BrandBar";
import { BudgetRevealSection, BudgetRevealSectionProps } from "./components/xiaojin/BudgetRevealSection";
import { Calendar, CalendarProps } from "./components/xiaojin/Calendar";
import { Captions, CaptionPhrase } from "./components/xiaojin/Captions";
import { ChapterNav, Chapter } from "./components/xiaojin/ChapterNav";
import { Layer, contentZ, DEFAULT_CONTENT_LAYER } from "./components/xiaojin/Layer";
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
  /** Editor-only position override — see ComplianceBar.tsx's own defaults. */
  x?: number;
  y?: number;
  width?: number;
}

export interface BrandInfo {
  company: string;
  label: string;
  /** Editor-only position override — see BrandBar.tsx's own defaults. */
  x?: number;
  y?: number;
  width?: number;
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
  /** Editor-only position override, "chips" variant only — see
   *  ChipsIntro.tsx's own defaults. */
  x?: number;
  y?: number;
}

export interface ChapterNavPosition {
  x?: number;
  y?: number;
  width?: number;
}

export interface RainbowBarPosition {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
}

// `& { layer?: number }` on each: Phase E's optional stacking-order override
// (contracts/render_props.schema.json's per-item `layer` field). Purely a
// type-level addition — these fields already flow through {...item} spreads
// at runtime regardless of this annotation; this just lets XiaojinEditorial
// read `item.layer` before spreading. QRContact/CornerCardItem deliberately
// excluded — they're not in the content z-order band (see Layer.tsx).
/** Matches contract②'s dataCards item shape exactly — colorMode/fonts come from the parent. */
export type DataCard = Omit<InfoCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type Gauge = Omit<RiskGaugeProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type BeforeAfter = Omit<BudgetRevealSectionProps, "headingFont" | "labelFont"> & { layer?: number };
export type Countdown = Omit<CountdownRingProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type CalendarEvent = Omit<CalendarProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type Pill = Omit<AccentPillProps, "colorMode" | "headingFont"> & { layer?: number };
export type QRContact = Omit<QRContactCardProps, "colorMode" | "headingFont" | "labelFont">;
export type Quote = Omit<QuoteCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type ZoneHeaderItem = Omit<ZoneHeaderProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type StepListItem = Omit<StepListProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type TopicCardItem = Omit<TopicCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type CornerCardItem = CornerCardProps;
// "CardItem" (not "Item") suffix deliberately, matching CornerCardItem — these
// are the outer per-card props for the props array, and Ranked/Checklist/
// IconCluster each already export their OWN per-row "...Item" type from their
// own component file (RankedListItem/ChecklistItem/IconClusterItem); reusing
// that exact name here for the outer card type would shadow-confuse the two.
export type ComparisonCardItem = Omit<ComparisonCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type RankedListCardItem = Omit<RankedListCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type ChecklistCardItem = Omit<ChecklistCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type LocationPinCardItem = Omit<LocationPinCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type TestimonialCardItem = Omit<TestimonialCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type IconClusterCardItem = Omit<IconClusterCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type ProgressBarCardItem = Omit<ProgressBarCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type ProsConsCardItem = Omit<ProsConsCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type MilestoneTrackCardItem = Omit<MilestoneTrackCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type TrustBadgeCardItem = Omit<TrustBadgeCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type BarChartCardItem = Omit<BarChartCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };
export type MilestoneUnlockCardItem = Omit<MilestoneUnlockCardProps, "colorMode" | "headingFont" | "labelFont"> & { layer?: number };

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
  /** Full SOURCE asset duration — always the untrimmed length; see videoCuts. */
  durationSeconds: number;
  /** Editor-authored razor cuts (source-video frames). Absent/empty = untrimmed. See ./cuts.ts. */
  videoCuts?: import("./cuts").VideoCut[];
  /** Speaker video playback volume, 0-1. Absent/undefined = full volume (every job before this field existed is unaffected). Does not affect the music bed — that's mixed post-render, see whatsapp_mvp/pipeline_runner.py's musicVolume handling. */
  videoVolume?: number;
  colorMode: ColorMode;
  /** Calibrated per source video — see SpeakerCard's doc comment. Required, no safe default. */
  speakerObjectPosition: string;
  scenes: SpeakerCardScene[];
  opacityKeyframes?: SpeakerCardOpacityKeyframe[];
  chapters: Chapter[];
  introOutFrame: number;
  captions: CaptionPhrase[];
  /** Optional editor override for where captions render — single-entry array,
   *  see Captions.tsx's own doc comment. Omit for the original centered-bottom
   *  position. */
  captionPosition?: { x: number; y: number; width?: number }[];
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
  /** Editor-only position override for the top chapter-nav bar. Not
   *  authored by the pipeline. */
  chapterNav?: ChapterNavPosition;
  /** Editor-only position override for the bottom progress bar. Not
   *  authored by the pipeline. */
  rainbowBar?: RainbowBarPosition;
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
  // Single source of truth (src/cuts.ts) — do not hand-recompute this
  // formula here. durationSeconds keeps meaning "full source asset
  // duration" (stays server-pinned) and is untouched; with no videoCuts
  // this is a strict no-op against the pre-cuts formula (ceil(durationSeconds*fps)).
  const durationInFrames = computeOutputDuration(props);
  return {
    durationInFrames,
    fps,
    width: 1080,
    height: 1920,
  };
};

export const XiaojinEditorial: React.FC<XiaojinEditorialProps> = (rawProps) => {
  const sourceDurationFrames = Math.max(1, Math.ceil((rawProps.durationSeconds || 1) * 30));
  const cuts = normalizeCuts(rawProps.videoCuts, sourceDurationFrames);
  // Map every time-bearing field from SOURCE to OUTPUT coordinates once,
  // here, before any card destructures/renders — so none of the ~40
  // individual card components below need to know cuts exist at all; they
  // just see already-correct mountFrame/endFrame/etc. values. With no cuts
  // this is a strict, reference-preserving no-op (see mapPropsForCuts's own
  // doc comment) — every job before this feature existed renders unchanged.
  const props = mapPropsForCuts(rawProps, cuts);
  const {
    videoSrc,
    videoVolume,
    colorMode,
    speakerObjectPosition,
    scenes,
    opacityKeyframes,
    chapters,
    introOutFrame,
    captions,
    captionPosition,
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
    chapterNav,
    rainbowBar,
    headingFont = _defaultHeadingFont,
    labelFont = _defaultLabelFont,
  } = props;
  const bg = colorMode === "warm" ? "#F2EBE0" : "#0D1117";

  // Deliberately called here, not at module scope: getRemotionEnvironment()
  // only reports isPlayer correctly once <Player>'s own component body has
  // run and set window.remotion_isPlayer — which happens before this
  // component renders (parent-before-child) but NOT before this module's
  // top-level code runs at import time. A module-scope loadFont() call
  // always saw isPlayer as false (confirmed live — it still fired all 306
  // chinese-traditional requests even with an isPlayer branch in place),
  // which is why this moved down into the component instead. loadFont()'s
  // own internal cache makes the useMemo a nicety, not a correctness
  // requirement — repeated calls with the same weights/subsets are free.
  useMemo(() => {
    const { isPlayer } = getRemotionEnvironment();
    loadNotoSansTC("normal", {
      weights: isPlayer ? ["700"] : ["500", "700", "800"],
      subsets: isPlayer ? ["latin"] : ["chinese-traditional", "latin"],
    });
    loadInter("normal", {
      weights: ["400", "500", "600", "700", "800"],
      subsets: ["latin"],
    });
  }, []);

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
        videoVolume={videoVolume}
        videoCuts={cuts}
        sourceDurationFrames={sourceDurationFrames}
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
        <Layer key={i} z={contentZ(header.layer, DEFAULT_CONTENT_LAYER.zoneHeaders)}>
          <ZoneHeader {...header} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {quotes?.map((q, i) => (
        <Layer key={`q${i}`} z={contentZ(q.layer, DEFAULT_CONTENT_LAYER.quotes)}>
          <QuoteCard {...q} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {dataCards?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.dataCards)}>
          <InfoCard
            {...card}
            colorMode={colorMode}
            headingFont={headingFont}
            labelFont={labelFont}
          />
        </Layer>
      ))}
      {beforeAfter?.map((reveal, i) => (
        <Layer key={i} z={contentZ(reveal.layer, DEFAULT_CONTENT_LAYER.beforeAfter)}>
          <BudgetRevealSection
            {...reveal}
            headingFont={headingFont}
            labelFont={labelFont}
          />
        </Layer>
      ))}
      {gauges?.map((gauge, i) => (
        <Layer key={i} z={contentZ(gauge.layer, DEFAULT_CONTENT_LAYER.gauges)}>
          <RiskGauge
            {...gauge}
            colorMode={colorMode}
            headingFont={headingFont}
            labelFont={labelFont}
          />
        </Layer>
      ))}
      {countdowns?.map((countdown, i) => (
        <Layer key={i} z={contentZ(countdown.layer, DEFAULT_CONTENT_LAYER.countdowns)}>
          <CountdownRing
            {...countdown}
            colorMode={colorMode}
            headingFont={headingFont}
            labelFont={labelFont}
          />
        </Layer>
      ))}
      {calendarEvents?.map((event, i) => (
        <Layer key={i} z={contentZ(event.layer, DEFAULT_CONTENT_LAYER.calendarEvents)}>
          <Calendar
            {...event}
            colorMode={colorMode}
            headingFont={headingFont}
            labelFont={labelFont}
          />
        </Layer>
      ))}
      {pills?.map((pill, i) => (
        <Layer key={i} z={contentZ(pill.layer, DEFAULT_CONTENT_LAYER.pills)}>
          <AccentPill
            {...pill}
            colorMode={colorMode}
            headingFont={headingFont}
          />
        </Layer>
      ))}
      {stepLists?.map((list, i) => (
        <Layer key={i} z={contentZ(list.layer, DEFAULT_CONTENT_LAYER.stepLists)}>
          <StepList {...list} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {topicCards?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.topicCards)}>
          <TopicCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {comparisons?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.comparisons)}>
          <ComparisonCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {rankedLists?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.rankedLists)}>
          <RankedListCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {checklists?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.checklists)}>
          <ChecklistCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {locationPins?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.locationPins)}>
          <LocationPinCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {testimonials?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.testimonials)}>
          <TestimonialCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {iconClusters?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.iconClusters)}>
          <IconClusterCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {progressBars?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.progressBars)}>
          <ProgressBarCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {prosCons?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.prosCons)}>
          <ProsConsCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {milestoneTracks?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.milestoneTracks)}>
          <MilestoneTrackCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {trustBadges?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.trustBadges)}>
          <TrustBadgeCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {barCharts?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.barCharts)}>
          <BarChartCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
      ))}
      {milestoneUnlocks?.map((card, i) => (
        <Layer key={i} z={contentZ(card.layer, DEFAULT_CONTENT_LAYER.milestoneUnlocks)}>
          <MilestoneUnlockCard {...card} colorMode={colorMode} headingFont={headingFont} labelFont={labelFont} />
        </Layer>
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
          x={intro.x}
          y={intro.y}
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
      {presenter ? (
        <Presenter
          {...presenter}
          colorMode={colorMode}
          videoCuts={cuts}
          sourceDurationFrames={sourceDurationFrames}
        />
      ) : null}
      <ChapterNav
        chapters={chapters}
        introOutFrame={introOutFrame}
        colorMode={colorMode}
        headingFont={headingFont}
        labelFont={labelFont}
        x={chapterNav?.x}
        y={chapterNav?.y}
        width={chapterNav?.width}
      />
      <Captions
        captions={captions}
        introOutFrame={introOutFrame}
        colorMode={colorMode}
        font={headingFont}
        position={captionPosition?.[0]}
      />
      {compliance ? (
        <ComplianceBar {...compliance} font={headingFont} />
      ) : brand ? (
        <BrandBar {...brand} colorMode={colorMode} font={headingFont} />
      ) : null}
      <RainbowProgressBar
        x={rainbowBar?.x}
        y={rainbowBar?.y}
        width={rainbowBar?.width}
        height={rainbowBar?.height}
      />
    </AbsoluteFill>
  );
};
