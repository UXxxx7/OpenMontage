import { itemTimeRange, CAPTION_POSITION_SECTION } from "./model";
import { isDraggableSection } from "./positioning";
import { estimatedRect, type Rect } from "./geometry";
import { effectiveLayer } from "./layers";
import { STATIC_ELEMENTS, type StaticHit } from "./staticElements";
import type { VideoCut } from "../../src/cuts";

// index: null identifies a top-level OBJECT field (a "static element" —
// see staticElements.ts), not an array item.
export type HitTarget = { section: string; index: number | null };

// A drawn rect shorter than this is still real geometry (the rainbow
// progress bar really is 8px tall) but too thin to reliably click — the
// TEST rect is inflated around the same center; the DRAWN box (hover
// outline / selection box) always uses the true rect, so this never lies
// about geometry, only about how forgiving a click needs to be.
const MIN_THIN_HIT_H = 24;

function inflateForHit(rect: Rect, hit: StaticHit): Rect {
  if (hit !== "thin" || rect.h >= MIN_THIN_HIT_H) return rect;
  const pad = (MIN_THIN_HIT_H - rect.h) / 2;
  return { x: rect.x, y: rect.y - pad, w: rect.w, h: MIN_THIN_HIT_H };
}

/**
 * All selectable elements under a composition-space point at the given
 * OUTPUT frame, TOPMOST FIRST — the basis for click-to-select on the video
 * preview. Pure function, no React: PreviewOverlay calls this from its
 * pointerdown handler, nothing subscribes to it.
 *
 * Two families of candidate, gathered separately then merged:
 *  - SOLID: real, clickable elements — the ~22 content cards (unchanged
 *    mechanism) plus "solid"-kind static elements (chrome bars, chips
 *    intro, the facecam, corner cards, qrContact, presenter).
 *  - BACKDROP: full-canvas or near-full-canvas static elements (a section
 *    takeover, the outro, 3 of the 4 intro variants) — click-THROUGH by
 *    default so a card sitting on top of one still wins the first click;
 *    only reachable by clicking empty space, or by the existing
 *    click-again-to-go-deeper cycling (PreviewOverlay.tsx's own
 *    clickCycleRef) once nothing solid is under the pointer at that spot.
 *  solid always sorts before backdrop regardless of z, which is what makes
 *  backdrops click-through — see the return statement.
 *
 * "Topmost" within each family = highest z (content cards keep their
 * existing effectiveLayer 0-99 band; static elements use their own z from
 * staticElements.ts, negative for the handful that paint BEHIND content —
 * sections/scenes/cornerCards — and >=200 for the chrome that paints in
 * front of it, matching XiaojinEditorial.tsx's actual JSX paint order).
 * Ties fall back to declaration/array order, later wins (mirrors real
 * paint order: later-declared draws on top).
 */
export function hitTestAt(
  props: Record<string, unknown>,
  compX: number,
  compY: number,
  frame: number,
  cuts: readonly VideoCut[],
  durationInFrames: number
): HitTarget[] {
  const solid: (HitTarget & { z: number; order: number })[] = [];
  const backdrop: (HitTarget & { z: number; order: number })[] = [];
  let order = 0;

  const consider = (section: string, index: number | null, z: number, hit: StaticHit, rect: Rect | null) => {
    order++;
    if (!rect) return;
    const testRect = inflateForHit(rect, hit);
    if (compX < testRect.x || compX > testRect.x + testRect.w || compY < testRect.y || compY > testRect.y + testRect.h) return;
    const target = { section, index, z, order };
    (hit === "backdrop" ? backdrop : solid).push(target);
  };

  // 1) The ~22 plain-xy content cards — unchanged mechanism. captionPosition
  //    is excluded here even though it's plain-xy too — it gets its own
  //    rule (1.5) below so a click on it can surface the active caption
  //    PHRASE first, not just the box.
  for (const [section, value] of Object.entries(props)) {
    if (!isDraggableSection(section) || !Array.isArray(value) || section === CAPTION_POSITION_SECTION) continue;
    value.forEach((rawItem, index) => {
      if (!rawItem || typeof rawItem !== "object") { order++; return; }
      const item = rawItem as Record<string, unknown>;
      const range = itemTimeRange(section, item, durationInFrames, cuts);
      if (!range || frame < range.from || frame >= range.to) { order++; return; }
      consider(section, index, effectiveLayer(section, item), "solid", estimatedRect(section, item));
    });
  }

  // 1.5) captionPosition — a click inside the shared caption box selects
  //    the caption PHRASE that's actually on screen at this frame first
  //    (so its text opens in the Inspector), falling back to the box alone
  //    when no phrase is active (scrubbed to a gap between lines). Kept
  //    separate from rule 1 above precisely so this can push two stacked
  //    hits instead of one. "On screen" reuses itemTimeRange exactly like
  //    every other section, so it's the same window the timeline highlights,
  //    cuts included — not a hand-rolled ms comparison. The phrase hit's z
  //    is nudged a half-step above the box's own so it always wins the tie
  //    at the identical rect (PreviewOverlay's DRAG_PROXY then resolves a
  //    dragged/selected phrase back to this same box for the actual drag).
  {
    const cpArr = props[CAPTION_POSITION_SECTION];
    const cpItem = Array.isArray(cpArr) ? (cpArr[0] as Record<string, unknown> | undefined) : undefined;
    order++;
    if (cpItem) {
      const rect = estimatedRect(CAPTION_POSITION_SECTION, cpItem);
      const z = effectiveLayer(CAPTION_POSITION_SECTION, cpItem);
      const captionsArr = Array.isArray(props.captions) ? (props.captions as Record<string, unknown>[]) : [];
      let activeIndex: number | null = null;
      for (let i = 0; i < captionsArr.length; i++) {
        const cRange = itemTimeRange("captions", captionsArr[i], durationInFrames, cuts);
        if (cRange && frame >= cRange.from && frame < cRange.to) { activeIndex = i; break; }
      }
      if (activeIndex !== null) {
        consider("captions", activeIndex, z + 0.5, "solid", rect);
      }
      consider(CAPTION_POSITION_SECTION, 0, z, "solid", rect);
    }
  }

  // 2) Object-shaped static elements (index: null) — intro/outro/compliance/
  //    brand/rainbowBar/chapterNav/qrContact/presenter.
  for (const [section, entry] of Object.entries(STATIC_ELEMENTS)) {
    if (entry.shape !== "object") continue;
    if (!entry.present?.(props)) continue;
    if (!entry.visibleAt?.(props, frame, durationInFrames)) continue;
    consider(section, null, entry.z, entry.hit(props), entry.rect(props, undefined, frame));
  }

  // 3) Array-shaped static elements with a real per-item TimeDescriptor —
  //    sections[]/cornerCards[] already have fromFrame/toFrame or
  //    mountFrame/endFrame, so visibility reuses itemTimeRange exactly like
  //    the content cards above; only the rect is custom (no plain x/y).
  //    `scenes` is excluded here — see (4) below, it's not a normal array.
  for (const [section, entry] of Object.entries(STATIC_ELEMENTS)) {
    if (entry.shape !== "array" || section === "scenes") continue;
    const arr = props[section];
    if (!Array.isArray(arr)) continue;
    arr.forEach((rawItem, index) => {
      if (!rawItem || typeof rawItem !== "object") { order++; return; }
      const item = rawItem as Record<string, unknown>;
      const range = itemTimeRange(section, item, durationInFrames, cuts);
      if (!range || frame < range.from || frame >= range.to) { order++; return; }
      consider(section, index, entry.z, entry.hit(props), entry.rect(props, item, frame));
    });
  }

  // 4) scenes (facecam) — its items are KEYFRAMES of one visual element,
  //    not independent instances (unlike every other array here). Clicking
  //    the facecam selects the currently-ACTIVE keyframe (the last one at
  //    or before the playhead) as one representative hit, not all of them
  //    stacked as if they were N separate overlapping cards.
  {
    const entry = STATIC_ELEMENTS.scenes;
    const scenes = Array.isArray(props.scenes) ? (props.scenes as { frame: number }[]) : [];
    order++;
    if (scenes.length > 0 && entry.visibleAt!(props, frame, durationInFrames)) {
      let activeIndex = 0;
      for (let i = 0; i < scenes.length; i++) {
        if (scenes[i].frame <= frame) activeIndex = i; else break;
      }
      consider(
        "scenes", activeIndex, entry.z, entry.hit(props),
        entry.rect(props, scenes[activeIndex] as unknown as Record<string, unknown>, frame)
      );
    }
  }

  solid.sort((a, b) => (b.z !== a.z ? b.z - a.z : b.order - a.order));
  backdrop.sort((a, b) => (b.z !== a.z ? b.z - a.z : b.order - a.order));
  return [...solid, ...backdrop].map(({ section, index }) => ({ section, index }));
}

export function sameHitTarget(a: HitTarget | null, b: HitTarget | null): boolean {
  if (!a || !b) return a === b;
  return a.section === b.section && a.index === b.index;
}

export function sameHitStack(a: HitTarget[], b: HitTarget[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((h, i) => sameHitTarget(h, b[i]));
}
