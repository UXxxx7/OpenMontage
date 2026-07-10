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
import { AbsoluteFill, CalculateMetadataFunction } from "remotion";
import renderPropsSchema from "../../contracts/render_props.schema.json";
import { BrandBar } from "./components/xiaojin/BrandBar";
import { BudgetRevealSection, BudgetRevealSectionProps } from "./components/xiaojin/BudgetRevealSection";
import { Calendar, CalendarProps } from "./components/xiaojin/Calendar";
import { Captions, CaptionPhrase } from "./components/xiaojin/Captions";
import { ChapterNav, Chapter } from "./components/xiaojin/ChapterNav";
import { ComplianceBar } from "./components/xiaojin/ComplianceBar";
import { ContentBeat, ContentZone } from "./components/xiaojin/ContentZone";
import { Section, SectionLayer } from "./components/xiaojin/SectionLayer";
import { QuoteCard, QuoteCardProps } from "./components/xiaojin/QuoteCard";
import { Atmosphere } from "./components/xiaojin/Atmosphere";
import { CountdownRing, CountdownRingProps } from "./components/xiaojin/CountdownRing";
import { InfoCard, InfoCardProps } from "./components/xiaojin/InfoCard";
import { IntroTitle } from "./components/xiaojin/IntroTitle";
import { OutroSection } from "./components/xiaojin/OutroSection";
import { QRContactCard, QRContactCardProps } from "./components/xiaojin/QRContactCard";
import { RainbowProgressBar } from "./components/xiaojin/RainbowProgressBar";
import { RiskGauge, RiskGaugeProps } from "./components/xiaojin/RiskGauge";
import { SpeakerCard, SpeakerCardOpacityKeyframe, SpeakerCardScene } from "./components/xiaojin/SpeakerCard";
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
}

/** Matches contract②'s dataCards item shape exactly — colorMode/fonts come from the parent. */
export type DataCard = Omit<InfoCardProps, "colorMode" | "headingFont" | "labelFont">;
export type Gauge = Omit<RiskGaugeProps, "colorMode" | "headingFont" | "labelFont">;
export type BeforeAfter = Omit<BudgetRevealSectionProps, "headingFont" | "labelFont">;
export type Countdown = Omit<CountdownRingProps, "colorMode" | "headingFont" | "labelFont">;
export type CalendarEvent = Omit<CalendarProps, "colorMode" | "headingFont" | "labelFont">;
export type QRContact = Omit<QRContactCardProps, "colorMode" | "headingFont" | "labelFont">;
export type Quote = Omit<QuoteCardProps, "colorMode" | "headingFont" | "labelFont">;

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
  /** Faint drifting keyword texture behind everything (this video's own vocabulary). */
  atmosphereKeywords?: string[];
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
  atmosphereKeywords,
  dataCards,
  beforeAfter,
  gauges,
  countdowns,
  calendarEvents,
  qrContact,
  intro,
  outro,
  compliance,
  brand,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const bg = colorMode === "warm" ? "#F2EBE0" : "#0D1117";

  return (
    <AbsoluteFill style={{ background: bg }}>
      {atmosphereKeywords?.length ? (
        <Atmosphere keywords={atmosphereKeywords} colorMode={colorMode} headingFont={headingFont} />
      ) : null}
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
      />
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
      {qrContact ? (
        <QRContactCard
          {...qrContact}
          colorMode={colorMode}
          headingFont={headingFont}
          labelFont={labelFont}
        />
      ) : null}
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
      {intro ? (
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
