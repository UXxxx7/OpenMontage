import React from "react";
import { LibraryTile } from "./LibraryTile";
import { CARD_RENDERERS } from "../../../src/components/xiaojin/galleryRegistry";
import { PREVIEW_SAMPLES } from "../../previewSamples";
import { SECTION_GROUPS } from "../../fieldHints";

/**
 * CapCut-style visual element picker — a real, hover-animated preview per
 * card type instead of AddMenu's text-only dropdown. Grouped the same way
 * AddMenu already groups sections (SECTION_GROUPS), so the two stay
 * conceptually identical. Reused as-is by both the desktop tree (its own
 * left column) and the phone shell's "Add" sheet (Phase 6, F2) — same
 * component, different chrome around it.
 */
export function LibraryPanel({
  onAdd,
  className,
  hideHeader,
  isTouch,
}: {
  onAdd: (section: string) => void;
  className?: string;
  /** The phone shell's PhoneSheet already supplies an "Add to timeline"
   *  header of its own — a second one directly underneath would be
   *  redundant, not just untidy. */
  hideHeader?: boolean;
  /** Forwarded to LibraryTile — tap-to-preview vs tap-to-add (Phase 6, F3c). */
  isTouch: boolean;
}) {
  const groups = Object.entries(SECTION_GROUPS)
    .map(([group, sections]) => [
      group,
      sections.filter((s) => s in CARD_RENDERERS && s in PREVIEW_SAMPLES),
    ] as const)
    .filter(([, sections]) => sections.length > 0);

  return (
    <div className={`library app__library${className ? ` ${className}` : ""}`}>
      {!hideHeader && <div className="library__header">Add to timeline</div>}
      <div className="library__body">
        {groups.map(([group, sections]) => (
          <div className="library__group" key={group}>
            <div className="library__group-title">{group}</div>
            <div className="library__grid">
              {sections.map((section) => (
                <LibraryTile
                  key={section}
                  section={section}
                  item={PREVIEW_SAMPLES[section]}
                  onAdd={onAdd}
                  isTouch={isTouch}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// Re-exported so a future section added to the schema without a gallery
// sample yet degrades to "just doesn't show a tile" instead of a crash —
// consumers can check this before assuming every addable section has one.
export function hasLibraryTile(section: string): boolean {
  return section in CARD_RENDERERS && section in PREVIEW_SAMPLES;
}
