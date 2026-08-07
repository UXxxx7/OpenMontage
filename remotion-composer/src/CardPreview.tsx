import React from "react";
import { AbsoluteFill } from "remotion";
import { CARD_RENDERERS } from "./components/xiaojin/galleryRegistry";
import type { ColorMode } from "./components/xiaojin/theme";

/**
 * Isolated single-card composition for the Phase D animation gallery.
 * Deliberately minimal — no video, no font loading, no schema validation:
 * it exists purely so a gallery tile's <Thumbnail>/<Player> can render ONE
 * real card component on a plain background, exactly as it would look
 * inside a real video, without mounting the whole ~40-component
 * XiaojinEditorial tree. See galleryRegistry.tsx for why this uses its own
 * renderer map instead of reusing XiaojinEditorial.tsx's render tree.
 *
 * Not registered in Root.tsx — like XiaojinEditorial itself, this is
 * imported directly as a React component by the editor bundle and passed
 * to @remotion/player's <Thumbnail>/<Player> via their `component` prop,
 * which needs no Studio/CLI composition registry.
 */
export interface CardPreviewProps extends Record<string, unknown> {
  section: string;
  item: Record<string, unknown>;
  colorMode?: ColorMode;
}

export const CardPreview: React.FC<CardPreviewProps> = ({ section, item, colorMode = "warm" }) => {
  // Same formula as XiaojinEditorial.tsx's own `bg` — kept in sync by hand,
  // it's a two-value ternary, not worth a shared import for.
  const bg = colorMode === "warm" ? "#F2EBE0" : "#0D1117";
  const renderer = CARD_RENDERERS[section];

  return (
    <AbsoluteFill style={{ background: bg }}>
      {renderer ? renderer(item, { colorMode, headingFont: "Inter", labelFont: "Inter" }) : null}
    </AbsoluteFill>
  );
};
