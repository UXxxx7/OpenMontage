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
import { AbsoluteFill } from "remotion";
import { BrandBar } from "./components/xiaojin/BrandBar";
import { Captions, CaptionPhrase } from "./components/xiaojin/Captions";
import { ChapterNav, Chapter } from "./components/xiaojin/ChapterNav";
import { ComplianceBar } from "./components/xiaojin/ComplianceBar";
import { ContentBeat, ContentZone } from "./components/xiaojin/ContentZone";
import { IntroTitle } from "./components/xiaojin/IntroTitle";
import { OutroSection } from "./components/xiaojin/OutroSection";
import { RainbowProgressBar } from "./components/xiaojin/RainbowProgressBar";
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

export interface XiaojinEditorialProps {
  videoSrc: string;
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
      {contentBeats ? <ContentZone beats={contentBeats} /> : null}
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
      <SpeakerCard
        videoSrc={videoSrc}
        scenes={scenes}
        opacityKeyframes={opacityKeyframes}
        objectPosition={speakerObjectPosition}
        colorMode={colorMode}
      />
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
