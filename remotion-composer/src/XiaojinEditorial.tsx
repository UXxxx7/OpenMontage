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
 */
import { AbsoluteFill } from "remotion";
import { Captions, CaptionPhrase } from "./components/xiaojin/Captions";
import { ChapterNav, Chapter } from "./components/xiaojin/ChapterNav";
import { ComplianceBar } from "./components/xiaojin/ComplianceBar";
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
  /** Omit to render without the regulatory strip (non-regulated content). */
  compliance?: ComplianceInfo;
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
  compliance,
  headingFont = "inherit",
  labelFont = "inherit",
}) => {
  const bg = colorMode === "warm" ? "#F2EBE0" : "#0D1117";

  return (
    <AbsoluteFill style={{ background: bg }}>
      <SpeakerCard
        videoSrc={videoSrc}
        scenes={scenes}
        opacityKeyframes={opacityKeyframes}
        objectPosition={speakerObjectPosition}
        colorMode={colorMode}
      />
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
      {compliance ? <ComplianceBar {...compliance} font={headingFont} /> : null}
      <RainbowProgressBar />
    </AbsoluteFill>
  );
};
