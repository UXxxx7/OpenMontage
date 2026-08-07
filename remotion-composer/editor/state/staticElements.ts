import { W as NATIVE_W, H as NATIVE_H, NAV, COMPLIANCE, BRAND, PROGRESS } from "../../src/components/xiaojin/theme";
import { speakerRectAt, type SpeakerCardScene } from "../../src/components/xiaojin/SpeakerCard";
import type { Rect } from "./geometry";

/**
 * Hand-registered table of every on-screen element that ISN'T one of the
 * ~22 plain-x/y content cards `positioning.ts`/`geometry.ts` already cover
 * generically from the schema. The schema alone can't answer "where does
 * ComplianceBar render" (it's a hardcoded theme.ts constant, not a stored
 * field) or "what's the facecam's rect right now" (it's interpolated from
 * a keyframe array) — same reasoning as `layers.ts`'s DEFAULT_CONTENT_LAYER
 * and `positioning.ts`'s own HAND_REGISTERED table, kept as one small,
 * explicit list rather than derived.
 *
 * Two shapes:
 *  - "object": props[section] is a single top-level object (or absent) —
 *    selected as {section, index: null}. `present` gates whether it even
 *    exists in this job.
 *  - "array": props[section] is an array — selected as {section, index}
 *    like any normal card. Only listed here because it needs a CUSTOM rect
 *    (no plain x/y) and/or custom visibility (see `visibleAt`); sections
 *    that already have a normal TimeDescriptor (sections[], cornerCards[])
 *    omit `visibleAt` and fall back to the standard itemTimeRange-based
 *    check — only `scenes` (no natural on/off window; the facecam is always
 *    mounted) overrides it.
 */
export type StaticShape = "object" | "array";
export type StaticHit = "solid" | "thin" | "backdrop";

export interface StaticElementEntry {
  shape: StaticShape;
  /** Only consulted for shape:"object" — does props[section] exist at all? */
  present?: (props: Record<string, unknown>) => boolean;
  /** Omit to fall back to the standard TIME_DESCRIPTORS/itemTimeRange check
   *  (only correct for array-shaped entries that already have a real
   *  descriptor — sections[]/cornerCards[]). Object-shaped entries and
   *  `scenes` must supply this. */
  visibleAt?: (props: Record<string, unknown>, frame: number, durationInFrames: number) => boolean;
  /** Custom geometry. Needed whenever the element has no plain stored x/y
   *  (theme.ts constants, a computed facecam rect, a full-canvas backdrop).
   *  `item` is the selected array entry for shape:"array" (undefined for
   *  shape:"object", where the whole props[section] IS the item). */
  rect: (props: Record<string, unknown>, item: Record<string, unknown> | undefined, frame: number) => Rect | null;
  /** A function, not a constant, because `intro`'s hit kind depends on its
   *  OWN variant (chips is a real box; the other 3 are full-canvas
   *  backdrops) — every entry uses the same shape for consistency even
   *  though most just return a fixed value. */
  hit: (props: Record<string, unknown>) => StaticHit;
  /** Paint-order band — see hitTest.ts's chrome-vs-content sort. */
  z: number;
}

const FULL_CANVAS: Rect = { x: 0, y: 0, w: NATIVE_W, h: NATIVE_H };

// ChipsIntro has no stored size (fit-content flex column) — this estimates
// its typical footprint (up to MAX_CHIPS=4 chapter chips) purely for the
// selection hit box; not read by the render component at all.
const CHIPS_INTRO_RECT: Rect = { x: 96, y: 420, w: 360, h: 260 };

// CornerCard.tsx has no stored height either (content-sized). A reasonable
// fixed estimate, same convention as geometry.ts's own EST_HEIGHT table for
// content cards with no natural size to read.
const CORNER_CARD_W = 380;
const CORNER_CARD_EST_H = 170;
const CORNER_CARD_MARGIN = 20;

function objectItem(props: Record<string, unknown>, section: string): Record<string, unknown> | undefined {
  const v = props[section];
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

export const STATIC_ELEMENTS: Record<string, StaticElementEntry> = {
  chapterNav: {
    shape: "object",
    // Not a stored field — ChapterNav renders whenever there ARE chapters.
    // props.chapterNav itself (x/y/width) is a purely editor-authored
    // position override, injected with the current default at load time
    // (App.tsx's withDefaultObjectPosition) so it always has real data to
    // read/drag rather than needing the user to "create" it first.
    present: (props) => Array.isArray(props.chapters) && (props.chapters as unknown[]).length > 0,
    visibleAt: () => true, // opacity ramps in early, but it's mounted from frame 0
    rect: (props) => {
      const pos = objectItem(props, "chapterNav");
      const x = typeof pos?.x === "number" ? pos.x : 0;
      const y = typeof pos?.y === "number" ? pos.y : 0;
      const w = typeof pos?.width === "number" ? pos.width : NATIVE_W;
      return { x, y, w, h: NAV.h };
    },
    hit: () => "solid",
    z: 240,
  },
  compliance: {
    shape: "object",
    present: (props) => !!objectItem(props, "compliance"),
    visibleAt: () => true,
    rect: (props) => {
      const compliance = objectItem(props, "compliance");
      const x = typeof compliance?.x === "number" ? compliance.x : 0;
      const y = typeof compliance?.y === "number" ? compliance.y : COMPLIANCE.y;
      const w = typeof compliance?.width === "number" ? compliance.width : NATIVE_W;
      return { x, y, w, h: COMPLIANCE.h };
    },
    hit: () => "solid",
    z: 260,
  },
  brand: {
    shape: "object",
    // compliance/brand are mutually exclusive (schema $comment) — XiaojinEditorial
    // only ever mounts one, matching that here rather than double-registering a hit.
    present: (props) => !objectItem(props, "compliance") && !!objectItem(props, "brand"),
    visibleAt: () => true,
    rect: (props) => {
      const brand = objectItem(props, "brand");
      const x = typeof brand?.x === "number" ? brand.x : 0;
      const y = typeof brand?.y === "number" ? brand.y : BRAND.y;
      const w = typeof brand?.width === "number" ? brand.width : NATIVE_W;
      return { x, y, w, h: BRAND.h };
    },
    hit: () => "solid",
    z: 260,
  },
  rainbowBar: {
    shape: "object",
    present: () => true, // no stored field required — always rendered; see chapterNav's own comment on the default-injection pattern
    visibleAt: () => true,
    rect: (props) => {
      const pos = objectItem(props, "rainbowBar");
      const x = typeof pos?.x === "number" ? pos.x : 0;
      const y = typeof pos?.y === "number" ? pos.y : PROGRESS.y;
      const w = typeof pos?.width === "number" ? pos.width : NATIVE_W;
      const h = typeof pos?.height === "number" ? pos.height : PROGRESS.h;
      return { x, y, w, h };
    },
    hit: () => "thin",
    z: 270,
  },
  intro: {
    shape: "object",
    present: (props) => !!objectItem(props, "intro"),
    // All 4 variants: `frame <= introOutFrame + 12` (each component's own gate).
    visibleAt: (props, frame) => {
      const introOutFrame = typeof props.introOutFrame === "number" ? props.introOutFrame : 0;
      return frame <= introOutFrame + 12;
    },
    rect: (props) => {
      const intro = objectItem(props, "intro");
      if (intro?.variant !== "chips") return FULL_CANVAS;
      const x = typeof intro.x === "number" ? intro.x : CHIPS_INTRO_RECT.x;
      const y = typeof intro.y === "number" ? intro.y : CHIPS_INTRO_RECT.y;
      return { x, y, w: CHIPS_INTRO_RECT.w, h: CHIPS_INTRO_RECT.h };
    },
    // Depends on its OWN variant: chips is a real box worth clicking
    // directly (solid); the other 3 are full-canvas scrims, click-through
    // to whatever's on top of them (backdrop) — see hitTest.ts's own
    // comment on why backdrops matter (a card during the intro must win
    // the first click, not the scrim behind it).
    hit: (props) => (objectItem(props, "intro")?.variant === "chips" ? "solid" : "backdrop"),
    z: 220,
  },
  outro: {
    shape: "object",
    present: (props) => !!objectItem(props, "outro"),
    visibleAt: (props, frame) => {
      const outro = objectItem(props, "outro");
      const fromFrame = typeof outro?.fromFrame === "number" ? outro.fromFrame : 0;
      return frame >= fromFrame;
    },
    rect: () => ({ x: 0, y: 88, w: NATIVE_W, h: NATIVE_H - 88 - 72 }),
    hit: () => "backdrop",
    z: 200,
  },
  qrContact: {
    shape: "object",
    present: (props) => !!objectItem(props, "qrContact"),
    visibleAt: (props, frame) => {
      const qr = objectItem(props, "qrContact");
      const mountFrame = typeof qr?.mountFrame === "number" ? qr.mountFrame : 0;
      return frame >= mountFrame; // no endFrame — stays until the video ends
    },
    rect: (props) => {
      const qr = objectItem(props, "qrContact");
      const x = typeof qr?.x === "number" ? qr.x : 80;
      const y = typeof qr?.y === "number" ? qr.y : 780;
      const w = typeof qr?.width === "number" ? qr.width : 920;
      return { x, y, w, h: 260 }; // matches geometry.ts's EST_HEIGHT.qrContact
    },
    hit: () => "solid",
    z: 210,
  },
  presenter: {
    shape: "object",
    present: (props) => !!objectItem(props, "presenter"),
    visibleAt: (props, frame) => {
      const presenter = objectItem(props, "presenter");
      const windows = Array.isArray(presenter?.windows) ? (presenter!.windows as { fromFrame: number; toFrame: number }[]) : [];
      return windows.some((w) => frame >= w.fromFrame && frame < w.toFrame);
    },
    rect: (props) => {
      const presenter = objectItem(props, "presenter");
      if (!presenter) return null;
      const x = typeof presenter.x === "number" ? presenter.x : 0;
      const y = typeof presenter.y === "number" ? presenter.y : 0;
      const w = typeof presenter.w === "number" ? presenter.w : NATIVE_W;
      const h = typeof presenter.h === "number" ? presenter.h : NATIVE_H;
      return { x, y, w, h };
    },
    hit: () => "solid",
    z: 230,
  },

  // ── Array-shaped: selection is {section, index} like a normal card ──

  scenes: {
    shape: "array",
    // No natural on/off window (each keyframe's own TimeDescriptor is a
    // POINT, for the timeline's own marker display — NOT "is the facecam
    // on screen", which is always true once there's at least one scene).
    visibleAt: (props) => Array.isArray(props.scenes) && (props.scenes as unknown[]).length > 0,
    rect: (props, _item, frame) => speakerRectAt((props.scenes as SpeakerCardScene[] | undefined) ?? [], frame),
    hit: () => "solid",
    z: -5,
  },
  sections: {
    shape: "array",
    // visibleAt omitted — sections[] already has a real fromFrame/toFrame
    // TimeDescriptor, itemTimeRange already computes the right window.
    rect: () => FULL_CANVAS,
    hit: () => "backdrop",
    z: -10,
  },
  cornerCards: {
    shape: "array",
    // visibleAt omitted — cornerCards[] already has mountFrame/endFrame.
    // Anchored to the facecam's OWN live rect (bottom-left corner, see
    // CornerCard.tsx's own left:20,bottom:20 — it's rendered as a CHILD of
    // SpeakerCard specifically so it travels/scales with it).
    rect: (props, _item, frame) => {
      const speaker = speakerRectAt((props.scenes as SpeakerCardScene[] | undefined) ?? [], frame);
      if (!speaker) return null;
      return {
        x: speaker.x + CORNER_CARD_MARGIN,
        y: speaker.y + speaker.h - CORNER_CARD_MARGIN - CORNER_CARD_EST_H,
        w: CORNER_CARD_W,
        h: CORNER_CARD_EST_H,
      };
    },
    hit: () => "solid",
    z: -4, // just above the facecam it's anchored to (still below all content)
  },
};

export function isStaticSection(section: string): boolean {
  return section in STATIC_ELEMENTS;
}
