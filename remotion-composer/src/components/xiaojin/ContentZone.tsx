/**
 * Generic beat-driven content slot — the "graphic content" side that
 * appears opposite the floating SpeakerCard.
 *
 * Every video-studio project (vell-renewal-reminder, chris-quote,
 * iman-watches) has its own ContentZone.tsx, but each one is just a
 * frame-range switch statement hardcoded to that project's own bespoke
 * section components (RenewalSection, QuoteSection, etc.) imported from a
 * per-project generated/timeline module. Those section components are
 * inherently one-off content (a calendar graphic, a quote card, a watch
 * collection reveal) — not reusable, so they were not ported.
 *
 * This component generalizes the *pattern* instead: it takes a `beats`
 * array where each beat supplies its own render function, so a caller
 * assembles their own bespoke sections as beats without needing to fork
 * this file. Renders nothing during gap frames (the background shows
 * through), matching the source behavior.
 */
import { useCurrentFrame } from "remotion";

export interface ContentBeat {
  id: string;
  start: number;
  end: number;
  render: () => React.ReactNode;
}

export interface ContentZoneProps {
  beats: ContentBeat[];
}

export const ContentZone: React.FC<ContentZoneProps> = ({ beats }) => {
  const frame = useCurrentFrame();
  const active = beats.find((b) => frame >= b.start && frame < b.end);
  if (!active) return null;
  return <>{active.render()}</>;
};
