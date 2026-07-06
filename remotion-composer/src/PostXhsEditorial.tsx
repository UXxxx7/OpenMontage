/**
 * PostXhsEditorial — the CURRENT canonical xiaojin-editorial composition,
 * per VeLL-lab/video-studio's tools/directors/compose-director.md
 * ("post-xhs style": Dominant/Workflow SpeakerCard modes, 48px karaoke
 * captions). This supersedes the `XiaojinEditorial` composition built
 * earlier in this project's other branch (video-studio-style-integration),
 * which was based on the older vell-renewal-reminder ("pre-xhs") layout.
 *
 * Scope for this first pass: layer stack per compose-director.md minus
 * IntroTitle and OutroCTA (not needed for a short found-footage clip with
 * no branded open/close) — SpeakerCard, ContentZone (InfoCards only, no
 * StepCards/PipelineChips yet), Chrome, Captions.
 */
import { AbsoluteFill, CalculateMetadataFunction } from "remotion";
import { BrandBar } from "./components/postxhs/BrandBar";
import { Captions, CaptionPhrase } from "./components/postxhs/Captions";
import { Chapter, ChapterNav } from "./components/postxhs/ChapterNav";
import { InfoCard, InfoCardProps } from "./components/postxhs/InfoCard";
import { RainbowProgressBar } from "./components/postxhs/RainbowProgressBar";
import { SpeakerCard, SpeakerModeEntry } from "./components/postxhs/SpeakerCard";
import { COLOR } from "./components/postxhs/theme";

export interface PostXhsEditorialProps {
  videoSrc: string;
  /**
   * Real duration of `videoSrc` in seconds — drives durationInFrames via
   * calculateMetadata below. This composition is reused across arbitrary
   * source videos (not just the MrBeast demo), so duration MUST come from
   * props, never be hardcoded on the <Composition> registration in Root.tsx.
   */
  durationSeconds: number;
  sourceWidth: number;
  sourceHeight: number;
  dominantObjPos: number;
  workflowObjPos?: number;
  modeSchedule?: SpeakerModeEntry[];
  chapters: Chapter[];
  captions: CaptionPhrase[];
  infoCards: Omit<InfoCardProps, "font">[];
  brand: { name: string; tagline?: string };
  font?: string;
}

export const calculatePostXhsEditorialMetadata: CalculateMetadataFunction<
  PostXhsEditorialProps
> = async ({ props }) => {
  const fps = 30;
  return {
    durationInFrames: Math.max(1, Math.ceil((props.durationSeconds || 1) * fps)),
    fps,
    width: 1080,
    height: 1920,
  };
};

export const PostXhsEditorial: React.FC<PostXhsEditorialProps> = ({
  videoSrc,
  sourceWidth,
  sourceHeight,
  dominantObjPos,
  workflowObjPos,
  modeSchedule,
  chapters,
  captions,
  infoCards,
  brand,
  font = "inherit",
}) => {
  return (
    <AbsoluteFill style={{ background: COLOR.cream }}>
      <SpeakerCard
        videoSrc={videoSrc}
        sourceWidth={sourceWidth}
        sourceHeight={sourceHeight}
        dominantObjPos={dominantObjPos}
        workflowObjPos={workflowObjPos}
        modeSchedule={modeSchedule}
      />
      {infoCards.map((card, i) => (
        <InfoCard key={i} {...card} font={font} />
      ))}
      <ChapterNav chapters={chapters} font={font} />
      <BrandBar name={brand.name} tagline={brand.tagline} font={font} />
      <Captions captions={captions} font={font} />
      <RainbowProgressBar />
    </AbsoluteFill>
  );
};
