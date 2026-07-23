import { Composition, CalculateMetadataFunction } from "remotion";
import { Explainer, ExplainerProps } from "./Explainer";
import {
  CinematicRenderer,
  calculateCinematicMetadata,
} from "./CinematicRenderer";
import { signalFromTomorrowWithMusicFixture } from "./cinematic/fixtures";
import { TalkingHead, TalkingHeadProps } from "./TalkingHead";
import {
  TitledVideo,
  calculateTitledVideoMetadata,
} from "./TitledVideo";
import { EndTag, EndTagProps } from "./components/EndTag";
import { HeroTitle } from "./components/HeroTitle";
import { ProductReveal, ProductRevealProps } from "./components/ProductReveal";
import { CaptionOverlay, WordCaption } from "./components/CaptionOverlay";
import { CollageBurst, CollageBurstProps } from "./CollageBurst";
import { LyricOverlay, LyricOverlayProps } from "./LyricOverlay";
import {
  ReferenceStyleEdit,
  ReferenceStyleEditProps,
  calculateReferenceStyleEditMetadata,
} from "./ReferenceStyleEdit";
import { XiaojinEditorial, XiaojinEditorialProps, calculateXiaojinEditorialMetadata } from "./XiaojinEditorial";
import { NewGraphicsDemo } from "./NewGraphicsDemo";
import { NewGraphicsDemo2 } from "./NewGraphicsDemo2";

// ---------------------------------------------------------------------------
// Theme System — prevents every video from looking like dark fintech
// ---------------------------------------------------------------------------

export interface ThemeConfig {
  primaryColor: string;
  accentColor: string;
  backgroundColor: string;
  surfaceColor: string;
  textColor: string;
  mutedTextColor: string;
  headingFont: string;
  bodyFont: string;
  monoFont: string;
  chartColors: string[];
  springConfig: { damping: number; stiffness: number; mass: number };
  transitionDuration: number;
  captionHighlightColor: string;
  captionBackgroundColor: string;
}

export const THEMES: Record<string, ThemeConfig> = {
  "clean-professional": {
    primaryColor: "#2563EB",
    accentColor: "#F59E0B",
    backgroundColor: "#FFFFFF",
    surfaceColor: "#F9FAFB",
    textColor: "#1F2937",
    mutedTextColor: "#6B7280",
    headingFont: "Inter",
    bodyFont: "Inter",
    monoFont: "JetBrains Mono",
    chartColors: ["#2563EB", "#F59E0B", "#10B981", "#8B5CF6", "#EC4899", "#06B6D4"],
    springConfig: { damping: 20, stiffness: 120, mass: 1 },
    transitionDuration: 0.4,
    captionHighlightColor: "#2563EB",
    captionBackgroundColor: "rgba(255, 255, 255, 0.85)",
  },
  "flat-motion-graphics": {
    primaryColor: "#7C3AED",
    accentColor: "#EC4899",
    backgroundColor: "#0F172A",
    surfaceColor: "#1E293B",
    textColor: "#F8FAFC",
    mutedTextColor: "#94A3B8",
    headingFont: "Space Grotesk",
    bodyFont: "Space Grotesk",
    monoFont: "Fira Code",
    chartColors: ["#7C3AED", "#EC4899", "#06B6D4", "#F59E0B", "#10B981", "#EF4444"],
    springConfig: { damping: 12, stiffness: 80, mass: 1 },
    transitionDuration: 0.3,
    captionHighlightColor: "#22D3EE",
    captionBackgroundColor: "rgba(15, 23, 42, 0.75)",
  },
  "minimalist-diagram": {
    primaryColor: "#1A1A2E",
    accentColor: "#E94560",
    backgroundColor: "#FAFAFA",
    surfaceColor: "#FFFFFF",
    textColor: "#1A1A2E",
    mutedTextColor: "#6B7280",
    headingFont: "IBM Plex Sans",
    bodyFont: "IBM Plex Sans",
    monoFont: "IBM Plex Mono",
    chartColors: ["#E94560", "#1A1A2E", "#0F3460", "#9CA3AF"],
    springConfig: { damping: 25, stiffness: 150, mass: 1 },
    transitionDuration: 0.5,
    captionHighlightColor: "#E94560",
    captionBackgroundColor: "rgba(250, 250, 250, 0.9)",
  },
  "anime-ghibli": {
    primaryColor: "#2D5016",
    accentColor: "#FFB347",
    backgroundColor: "#0A0A1A",
    surfaceColor: "#1A2332",
    textColor: "#F0E6D3",
    mutedTextColor: "#A8957E",
    headingFont: "Noto Serif JP",
    bodyFont: "Noto Sans",
    monoFont: "Fira Code",
    chartColors: ["#FFB347", "#2D5016", "#FF6B9D", "#A8E6CF", "#6B4C8A", "#E8927C"],
    springConfig: { damping: 18, stiffness: 60, mass: 1 },
    transitionDuration: 1.0,
    captionHighlightColor: "#FFB347",
    captionBackgroundColor: "rgba(10, 10, 26, 0.8)",
  },
};

// Default theme when none is specified — uses the existing dark style for backwards compatibility
export const DEFAULT_THEME = THEMES["flat-motion-graphics"];

export function resolveTheme(props: Record<string, unknown>): ThemeConfig {
  const themeName = (props.theme as string) || (props.playbook as string);
  if (themeName && THEMES[themeName]) {
    return THEMES[themeName];
  }
  // Allow custom theme passed as full object
  if (props.themeConfig && typeof props.themeConfig === "object") {
    return { ...DEFAULT_THEME, ...(props.themeConfig as Partial<ThemeConfig>) };
  }
  return DEFAULT_THEME;
}

const calculateMetadata: CalculateMetadataFunction<ExplainerProps> = async ({
  props,
}) => {
  const cuts = props.cuts || [];
  if (cuts.length === 0) {
    return { durationInFrames: 30 * 60 };
  }
  const lastEnd = Math.max(...cuts.map((c) => c.out_seconds || 0));
  // Add 1 second padding for final fade
  return { durationInFrames: Math.ceil((lastEnd + 1) * 30) };
};

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="Explainer"
        component={Explainer}
        durationInFrames={30 * 60}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          cuts: [],
          overlays: [],
          captions: [],
          audio: {},
        }}
        calculateMetadata={calculateMetadata}
      />
      <Composition
        id="CinematicRenderer"
        component={CinematicRenderer}
        durationInFrames={30 * 30}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          scenes: [],
          titleFontSize: 78,
          titleWidth: 1320,
          signalLineCount: 18,
        }}
        calculateMetadata={calculateCinematicMetadata}
      />
      <Composition
        id="SignalFromTomorrowWithMusic"
        component={CinematicRenderer}
        durationInFrames={30 * 30}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={signalFromTomorrowWithMusicFixture}
        calculateMetadata={calculateCinematicMetadata}
      />
      <Composition
        id="TalkingHead"
        component={TalkingHead}
        durationInFrames={30 * 300}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          videoSrc: "",
          captions: [],
          overlays: [],
          wordsPerPage: 4,
          fontSize: 52,
          highlightColor: "#22D3EE",
        }}
      />
      <Composition
        id="TitledVideo"
        component={TitledVideo}
        durationInFrames={30 * 60}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          videoSrc: "",
          tagline: "home is a verb.",
          taglineInSeconds: 53.5,
          taglineOutSeconds: undefined,
          topPx: 150,
          fontSize: 148,
          accentColor: "#F5C470",
        }}
        calculateMetadata={calculateTitledVideoMetadata}
      />
      <Composition
        id="HeroTitle"
        component={HeroTitle}
        durationInFrames={30 * 17}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          title: "THE CALIBRATORS",
          subtitle: "The People Who Define Reality",
        }}
      />
      <Composition
        id="ProductReveal"
        component={ProductReveal}
        durationInFrames={30 * 8}
        fps={30}
        width={1280}
        height={720}
        defaultProps={{
          productImage: "airnothing/product.png",
          productName: "AirNothing Pro Max Ultra",
          price: "Starting at $999",
          tagline: "Nothing included.",
          closer: "Less is nothing.",
          accentColor: "#00D4FF",
        } as ProductRevealProps}
      />
      <Composition
        id="ProductRevealVertical"
        component={ProductReveal}
        durationInFrames={30 * 8}
        fps={30}
        width={720}
        height={1280}
        defaultProps={{
          productImage: "airnothing/product.png",
          productName: "AirNothing Pro Max Ultra",
          price: "Starting at $999",
          tagline: "Nothing included.",
          closer: "Less is nothing.",
          accentColor: "#00D4FF",
        } as ProductRevealProps}
      />
      <Composition
        id="CaptionOverlayOnly"
        component={CaptionOverlay}
        durationInFrames={30 * 300}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          words: [] as WordCaption[],
          wordsPerPage: 3,
          fontSize: 58,
          highlightColor: "#FACC15",
          backgroundColor: "rgba(15, 23, 42, 0.75)",
        }}
      />
      <Composition
        id="CollageBurst"
        component={CollageBurst}
        durationInFrames={30 * 30}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          backgroundSrc: "",
          backgroundInSeconds: 0,
          curtainStartSeconds: 1.5,
          curtainEndSeconds: 3.0,
          clips: [],
        } as CollageBurstProps}
      />
      <Composition
        id="LyricOverlay"
        component={LyricOverlay}
        durationInFrames={30 * 28}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          videoSrc: "",
          lyrics: [],
          bottomY: 0.88,
        } as LyricOverlayProps}
      />
      <Composition
        id="WhatsAppReferenceEdit"
        component={ReferenceStyleEdit}
        durationInFrames={Math.ceil(46.11 * 30)}
        fps={30}
        width={1280}
        height={720}
        defaultProps={{
          videoSrc: "whatsapp-2026-06-26-edit/rough-cut.mp4",
          videoFit: "contain",
          durationSeconds: 46.11,
          sourceStartSeconds: 0,
          captions: [
            {
              start: 0.12,
              end: 3.18,
              text: "Hi there. It's David from Pacific Life.",
            },
            {
              start: 3.7,
              end: 11.0,
              text: "Quick reminder, your policy is coming up for renewal in 30 days on the 28th of July.",
            },
            {
              start: 11.0,
              end: 18.24,
              text: "Your current plan covers you for $1.5 million, and your annual premium is $8,400.",
            },
            {
              start: 18.24,
              end: 23.66,
              text: "I've put the full breakdown in this video so you have everything in one place.",
            },
            {
              start: 24.36,
              end: 26.08,
              text: "Renewing on time really matters.",
            },
            {
              start: 26.52,
              end: 34.98,
              text: "If your policy lapses, you'd have to go through underwriting again, which could affect both your coverage and your rate.",
            },
            {
              start: 35.64,
              end: 41.38,
              text: "If you have any questions, just WhatsApp me directly or scan the QR code below.",
            },
            {
              start: 41.68,
              end: 45.42,
              text: "I'll get back to you right away. Looking forward to keeping you and your family protected.",
            },
            { start: 45.62, end: 46.11, text: "Take care." },
          ],
          infoCues: [
            {
              start: 0.4,
              end: 6.8,
              label: "Policy renewal reminder",
              x: 76,
              y: 118,
              width: 410,
            },
            {
              start: 11.4,
              end: 18.5,
              label: "$1.5M coverage | $8,400 premium",
              x: 674,
              y: 132,
              width: 520,
            },
            {
              start: 18.9,
              end: 32.0,
              label: "Renew on time to avoid lapse",
              x: 674,
              y: 132,
              width: 480,
            },
            {
              start: 35.0,
              end: 42.8,
              label: "Questions? Scan the QR code",
              x: 674,
              y: 132,
              width: 470,
            },
          ],
          layoutCues: [
            { start: 0, end: 11.0, mode: "full" },
            { start: 11.0, end: 46.11, mode: "left" },
          ],
          navItems: [],
        } as ReferenceStyleEditProps}
        calculateMetadata={calculateReferenceStyleEditMetadata}
      />
      {/*
        XiaojinEditorial — the real xiaojin-editorial style (see this file's
        header comment for how it differs from WhatsAppReferenceEdit above).
        Default props use the same David/Pacific Life demo script as
        WhatsAppReferenceEdit, so the two can be diffed side by side on the
        same source content. Scene positions and objectPosition below are
        ported from video-studio's validated build for this exact video —
        do not reuse them for a different source video without recalibrating
        (see SpeakerCard's doc comment).
      */}
      <Composition
        id="XiaojinEditorial"
        component={XiaojinEditorial}
        calculateMetadata={calculateXiaojinEditorialMetadata}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          videoSrc: "whatsapp-2026-06-26-edit/rough-cut.mp4",
          durationSeconds: 44.9,
          colorMode: "warm",
          speakerObjectPosition: "50% 35%",
          scenes: [
            { frame: 0, x: 60, y: 104, w: 960, h: 1100 },
            { frame: 1200, x: 60, y: 104, w: 960, h: 1100 },
          ],
          chapters: [
            { atFrame: 0, label: "提醒", labelEn: "REMINDER" },
            { atFrame: 340, label: "保障", labelEn: "COVERAGE" },
            { atFrame: 700, label: "续保", labelEn: "RENEW" },
            { atFrame: 1000, label: "联系", labelEn: "CONTACT" },
          ],
          introOutFrame: 20,
          captions: [
            { text: "Hi there. It's David from Pacific Life.", startMs: 120, endMs: 3180 },
            { text: "Quick reminder, your policy is coming up for renewal in 30 days on the 28th of July.", startMs: 3700, endMs: 11000 },
            { text: "Your current plan covers you for $1.5 million, and your annual premium is $8,400.", startMs: 11000, endMs: 18240 },
            { text: "I've put the full breakdown in this video so you have everything in one place.", startMs: 18240, endMs: 23660 },
            { text: "Renewing on time really matters.", startMs: 24360, endMs: 26080 },
            { text: "If your policy lapses, you'd have to go through underwriting again, which could affect both your coverage and your rate.", startMs: 26520, endMs: 34980 },
            { text: "If you have any questions, just WhatsApp me directly or scan the QR code below.", startMs: 35640, endMs: 41380 },
            { text: "I'll get back to you right away. Looking forward to keeping you and your family protected.", startMs: 41680, endMs: 45420 },
            { text: "Take care.", startMs: 45620, endMs: 46110 },
          ],
        } as XiaojinEditorialProps}
      />
      <Composition
        id="EndTag"
        component={EndTag}
        // 5.5s at 30fps = 165 frames. Render CLI can override via --props.
        durationInFrames={165}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          text: "THE CITY KEEPS ITS OWN VIGIL.",
          palette: "cool_offwhite_on_black",
          fadeInSeconds: 0.6,
          holdSeconds: 4.3,
          fadeOutSeconds: 0.6,
        } as EndTagProps}
      />
      <Composition
        id="EndTagOverlay"
        component={EndTag}
        // 8.19s at 30fps = 246 frames. Render CLI can override via --props.
        // Intended to be composited on top of body footage, not concat'd.
        durationInFrames={246}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          text: "EARN THE LIGHT.",
          palette: "cool_offwhite_on_black",
          fadeInSeconds: 1.0,
          holdSeconds: 5.69,
          fadeOutSeconds: 1.5,
          overlay: true,
        } as EndTagProps}
      />
      <Composition
        id="NewGraphicsDemo"
        component={NewGraphicsDemo}
        durationInFrames={540}
        fps={30}
        width={1080}
        height={1920}
      />
      <Composition
        id="NewGraphicsDemo2"
        component={NewGraphicsDemo2}
        durationInFrames={540}
        fps={30}
        width={1080}
        height={1920}
      />
    </>
  );
};
