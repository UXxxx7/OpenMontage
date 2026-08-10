export type ManifestElement = {
  id: string;
  kind: "text_block" | "stat_card" | "image_swap" | "broll_window";
  layer: number;
  x: number;
  y: number;
  w: number;
  h: number;
  mountFrame?: number;
  endFrame?: number;
  /** Set by recoverManifest.ts for an id that scene.tsx reads via
   *  `props.overrides?.[id]?.<field>` but that the server's manifest.json
   *  never listed (a confirmed real bug — see that module's doc comment).
   *  Geometry (x/y/w/h/layer) is never real on a recovered element, so the
   *  UI must hide position/size controls for it rather than offer a drag
   *  that silently does nothing. */
  recovered?: boolean;
  [key: string]: unknown;
};

/** Is this element actually on screen at `frame`? Missing `endFrame` = open-ended
 *  (runs to the end of the video), mirroring state/model.ts's ClipItem.openEnded —
 *  same convention Arm A's own timeline already uses for a card with no end. */
export function isVisibleAtFrame(el: ManifestElement, frame: number): boolean {
  const mount = el.mountFrame ?? 0;
  if (frame < mount) return false;
  if (el.endFrame == null) return true;
  return frame < el.endFrame;
}

/**
 * Arm B's equivalent of state/hitTest.ts's `hitTestAt` — genuinely simpler
 * because the manifest already carries explicit x/y/w/h directly (no
 * estimatedRect inference from a fixed card-type vocabulary needed). Same
 * topmost-first ordering rule as Arm A: higher `layer` wins, ties broken by
 * later array position (mirrors paint order — a model that lists an
 * element later in the manifest is assumed to have drawn it later too).
 *
 * MUST filter by `frame` — confirmed real bug (not just a "polish gap" as
 * this function's own earlier comment claimed): several manifest entries
 * routinely share the same x/y/w/h footprint (real example: 5 of 6 cards in
 * one job all sat at x=90,w=900), because the model reuses one screen
 * region across sequential, non-overlapping-in-TIME beats. Without a frame
 * filter, clicking the card the user can actually see would silently select
 * whichever OTHER same-footprint element sorts first by layer/order — often
 * one that isn't even mounted yet, making every click look like it does
 * nothing.
 */
export function authoredHitTestAt(
  manifest: ManifestElement[], compX: number, compY: number, frame: number
): string[] {
  const hits: { id: string; z: number; order: number }[] = [];
  manifest.forEach((el, order) => {
    if (!isVisibleAtFrame(el, frame)) return;
    if (compX < el.x || compX > el.x + el.w || compY < el.y || compY > el.y + el.h) return;
    hits.push({ id: el.id, z: el.layer, order });
  });
  hits.sort((a, b) => (b.z !== a.z ? b.z - a.z : b.order - a.order));
  return hits.map((h) => h.id);
}

export function sameIdStack(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((id, i) => id === b[i]);
}
