import React, { useCallback, useEffect, useRef, useState } from "react";
import { normalizeCuts } from "../../../src/cuts";
import { itemTimeRange, itemAt, CAPTION_POSITION_SECTION } from "../../state/model";
import { isDraggableSection, isObjectDraggableSection, isTwoAxisResizeSection } from "../../state/positioning";
import { estimatedRect, type Rect } from "../../state/geometry";
import { STATIC_ELEMENTS } from "../../state/staticElements";
import { hitTestAt, sameHitTarget, sameHitStack, type HitTarget } from "../../state/hitTest";
import { frameRef, subscribePlayhead } from "../../state/playhead";
import { selectionRef, subscribeSelection, type Selection } from "../../state/selectionBridge";

function detectDesktopPointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return true;
  return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
}

const NATIVE_W = 1080;
const NATIVE_H = 1920;
const DRAG_THRESHOLD_PX = 3;
const SNAP_RADIUS_PX = 8;
const MIN_WIDTH = 100;
const MIN_HEIGHT = 100;
/** How close (in real screen px) a second click must land to the first to
 *  count as "the same spot" for click-again-to-go-deeper, rather than a
 *  fresh click that resets to the topmost element. Generous on purpose —
 *  a finger can't tap the identical pixel twice. */
const CLICK_CYCLE_RADIUS_PX = 14;

// "resize-wh" is presenter's own corner handle — the one section stored as
// x/y/w/h rather than x/y/width(+computed height), so it needs a genuine
// two-axis resize instead of the usual width-only edge.
type DragMode = "move" | "resize-w" | "resize-wh";

interface DragState {
  mode: DragMode;
  startClientX: number;
  startClientY: number;
  origX: number;
  origY: number;
  origW: number;
  origH: number;
  el: HTMLElement;
  moved: boolean;
  axisLock: "x" | "y" | null;
}

/**
 * Drag-to-position overlay for the ~22 content cards that carry plain
 * x/y(/width). Rendered as a sibling of <Player> inside .preview__player
 * (see PreviewPane.tsx) — deliberately its own component with its OWN
 * playhead subscription, not lifted into PreviewPane's props, so per-frame
 * playhead updates during playback never cause PreviewPane itself (and
 * therefore the whole ~40-component XiaojinEditorial tree it mounts via
 * <Player>) to re-render. Only ONE box is ever shown (the current
 * selection), so this stays a single absolutely-positioned div.
 *
 * Mirrors Timeline/useClipDrag.ts's drag contract on purpose: Pointer
 * Events + setPointerCapture, a small dead zone before a drag "counts",
 * in-flight position written straight to the DOM (never React state), and
 * exactly one commit on pointerup — the reasoning is identical (a
 * mid-drag React re-render here would be a full XiaojinEditorial
 * composition re-render, not just a cheap timeline reflow).
 */
export function PreviewOverlay({
  scale,
  props,
  durationInFrames,
  onItemChange,
  onSelect,
}: {
  /** CSS px per composition px — equal to PreviewPane's own effectiveZoom, since
   *  this overlay fills .preview__player exactly (inset:0 of an already-sized box). */
  scale: number | null;
  props: Record<string, unknown>;
  durationInFrames: number;
  onItemChange: (section: string, index: number | null, next: Record<string, unknown>) => void;
  onSelect: (target: HitTarget | null) => void;
}) {
  const [frame, setFrame] = useState(() => frameRef.current);
  useEffect(() => subscribePlayhead(setFrame), []);

  // Selection comes from the module-level bridge, not a prop — see
  // selectionBridge.ts's own comment for why: PreviewPane is memoized
  // specifically so a selection change never re-renders it.
  const [selection, setSelection] = useState<Selection>(() => selectionRef.current);
  useEffect(() => subscribeSelection(setSelection), []);

  // Capability flag — desktop-only concerns (hover preview, the resize
  // handle). Read once; capability doesn't change over a session in any way
  // this needs to react to live.
  //
  // Deliberately NOT used to gate MOVE-dragging the box itself (Phase 6,
  // F3a): unlike a timeline clip, this box only ever exists for the
  // CURRENTLY SELECTED item (selectionRef above) — there is no "unselected
  // box" to disambiguate against, so the same selected-only rule that
  // Timeline needs explicit per-clip logic for is satisfied here for free.
  // touch-action:none on .preview-overlay__box (styles.css) keeps a
  // finger-drag on it from fighting page scroll.
  const [isDesktopPointer] = useState(detectDesktopPointer);

  const sourceDurationFrames = Math.max(1, Math.ceil((Number(props.durationSeconds) || 1) * 30));
  const cuts = normalizeCuts(
    props.videoCuts as { fromFrame: number; toFrame: number }[] | undefined,
    sourceDurationFrames
  );

  const rootRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const hudRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const [hoverTarget, setHoverTarget] = useState<HitTarget | null>(null);
  const clickCycleRef = useRef<{ clientX: number; clientY: number; stack: HitTarget[]; cursor: number } | null>(null);

  const section = selection?.section;
  const index = selection?.index ?? null;

  // A caption phrase (`captions[i]`) carries text/timing, never x/y — there's
  // nothing on it to drag. Clicking one selects the phrase (so its text
  // opens in the Inspector, wired via hitTestAt's own caption rule) but the
  // box that's actually shown/dragged is the ONE shared `captionPosition[0]`
  // every phrase renders through. This proxy map is the entire mechanism:
  // everywhere below that used to read `section`/`index` to find drag
  // geometry now reads `dragSection`/`dragIndex` instead, while `section`/
  // `index` keep meaning "what's selected" for the Inspector/outline/cycling.
  const DRAG_PROXY: Record<string, string> = { captions: CAPTION_POSITION_SECTION };
  const dragSection = section != null ? (DRAG_PROXY[section] ?? section) : null;
  const dragIndex = section != null && DRAG_PROXY[section] ? 0 : index;

  // Static elements (chrome bars, intro/outro, the facecam, …) — most have
  // no drag at all (see the plan's "deferred" list), but a target still
  // needs SOME visible feedback when hovered/selected. The two that DO drag
  // (qrContact, presenter — object-shaped, index:null, not array items)
  // reuse this same resolver for their rect/visibility below rather than a
  // second implementation, since STATIC_ELEMENTS already carries their
  // correct defaults-aware geometry. "backdrop"-kind elements (a section
  // takeover, the outro, 3 of 4 intro variants) deliberately resolve to no
  // rect at all — a full-canvas border is noise, not feedback; Premiere/
  // Figma don't outline a selected background layer either.
  const resolveStaticRect = useCallback(
    (sec: string, idx: number | null): Rect | null => {
      const entry = STATIC_ELEMENTS[sec];
      if (!entry || entry.hit(props) === "backdrop") return null;
      return entry.rect(props, itemAt(props, sec, idx), frame);
    },
    [props, frame]
  );
  const resolveStaticVisible = useCallback(
    (sec: string, idx: number | null): boolean => {
      const entry = STATIC_ELEMENTS[sec];
      if (!entry) return false;
      if (entry.visibleAt) return entry.visibleAt(props, frame, durationInFrames);
      const it = itemAt(props, sec, idx);
      if (!it) return false;
      const range = itemTimeRange(sec, it, durationInFrames, cuts);
      return !!range && frame >= range.from && frame < range.to;
    },
    [props, frame, durationInFrames, cuts]
  );

  const item =
    dragSection != null && isDraggableSection(dragSection)
      ? (itemAt(props, dragSection, dragIndex) ?? null)
      : null;

  // qrContact/presenter have no schema TimeDescriptor (they're objects, not
  // arrays — model.ts's TIME_DESCRIPTORS only scans array fields) and their
  // real geometry doesn't fit estimatedRect's "width + computed height"
  // content-card model (presenter stores its own h; qrContact's fallback
  // width lives in STATIC_ELEMENTS, not the schema) — route both through
  // the same static resolver used for their read-only outline instead.
  const rect = item
    ? isObjectDraggableSection(dragSection!)
      ? resolveStaticRect(dragSection!, dragIndex)
      : estimatedRect(dragSection as string, item)
    : null;

  const visible =
    !!item &&
    !!rect &&
    !!dragSection &&
    (isObjectDraggableSection(dragSection)
      ? resolveStaticVisible(dragSection, dragIndex)
      : (() => {
          const range = itemTimeRange(dragSection, item!, durationInFrames, cuts);
          return !!range && frame >= range.from && frame < range.to;
        })());

  // Whether there's a real, draggable thing under `dragSection`/`dragIndex`
  // right now. For array sections `item` is only ever truthy when
  // `dragIndex` is a genuine number (itemAt's array branch requires one);
  // for object sections (qrContact, presenter) `dragIndex` is legitimately
  // null and `item` alone is what matters — so this replaces the old
  // `dragIndex == null` guards below, which predate object-shaped targets
  // and would otherwise block dragging qrContact/presenter entirely.
  const hasDragTarget = item != null && dragSection != null;

  const staticSelectedRect = section != null && !item ? resolveStaticRect(section, index) : null;
  const staticSelectedVisible = !!staticSelectedRect && section != null && resolveStaticVisible(section, index);

  const getSnapTargets = useCallback(
    (excludeSection: string | null, excludeIndex: number | null): { xs: number[]; ys: number[] } => {
      const xs = [0, 60, 80, NATIVE_W / 2];
      const ys = [0];
      for (const [sec, spec] of Object.entries(props)) {
        if (!isDraggableSection(sec) || !Array.isArray(spec)) continue;
        spec.forEach((it, i) => {
          if (sec === excludeSection && i === excludeIndex) return;
          const r = estimatedRect(sec, it as Record<string, unknown>);
          if (!r) return;
          const range = itemTimeRange(sec, it as Record<string, unknown>, durationInFrames, cuts);
          if (!range || frame < range.from || frame >= range.to) return;
          xs.push(r.x, r.x + r.w);
          ys.push(r.y, r.y + r.h);
        });
      }
      return { xs, ys };
    },
    [props, durationInFrames, cuts, frame]
  );

  const snap = (value: number, targets: number[], radiusComp: number, disabled: boolean): number => {
    if (disabled) return value;
    let best = value;
    let bestDist = radiusComp;
    for (const t of targets) {
      const d = Math.abs(value - t);
      if (d <= bestDist) {
        best = t;
        bestDist = d;
      }
    }
    return best;
  };

  const showHud = (el: HTMLElement, text: string) => {
    const hud = hudRef.current;
    if (!hud) return;
    const r = el.getBoundingClientRect();
    const parentR = hud.offsetParent?.getBoundingClientRect();
    hud.style.display = "block";
    hud.style.left = `${r.left - (parentR?.left ?? 0)}px`;
    hud.style.top = `${r.top - (parentR?.top ?? 0) - 26}px`;
    hud.textContent = text;
  };
  const hideHud = () => {
    if (hudRef.current) hudRef.current.style.display = "none";
  };

  // Click-to-select (Phase 6, F1). compClientToComposition converts a raw
  // client-space point into composition coordinates using the root
  // overlay's own measured box — the same scale math the drag handlers
  // already use, just anchored to the root instead of the (possibly
  // absent) selection box.
  const compClientToComposition = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const root = rootRef.current;
      if (!root || !scale) return null;
      const r = root.getBoundingClientRect();
      return { x: (clientX - r.left) / scale, y: (clientY - r.top) / scale };
    },
    [scale]
  );

  const handleCanvasClick = useCallback(
    (clientX: number, clientY: number) => {
      const comp = compClientToComposition(clientX, clientY);
      if (!comp) return;
      const stack = hitTestAt(props, comp.x, comp.y, frame, cuts, durationInFrames);

      const prev = clickCycleRef.current;
      const sameSpot =
        !!prev &&
        Math.hypot(clientX - prev.clientX, clientY - prev.clientY) <= CLICK_CYCLE_RADIUS_PX &&
        sameHitStack(stack, prev.stack);

      const cursor = sameSpot && stack.length > 0 ? (prev!.cursor + 1) % stack.length : 0;
      clickCycleRef.current = { clientX, clientY, stack, cursor };
      onSelect(stack.length > 0 ? stack[cursor] : null);
    },
    [compClientToComposition, props, frame, cuts, durationInFrames, onSelect]
  );

  // Hover preview of what a click would grab — desktop only (there is no
  // "hover" concept on touch; isDesktopPointer is the same hover:hover+
  // pointer:fine capability check everything else here uses).
  // Only setState when the identity actually changes: this handler runs on
  // every pointermove, and hitTestAt walks every draggable section, so
  // re-rendering on every pixel of movement would be real, avoidable cost.
  const handleCanvasPointerMove = useCallback(
    (clientX: number, clientY: number) => {
      if (!isDesktopPointer) return;
      const comp = compClientToComposition(clientX, clientY);
      if (!comp) return;
      const stack = hitTestAt(props, comp.x, comp.y, frame, cuts, durationInFrames);
      const top = stack[0] ?? null;
      setHoverTarget((cur) => (sameHitTarget(cur, top) ? cur : top));
    },
    [isDesktopPointer, compClientToComposition, props, frame, cuts, durationInFrames]
  );

  const clearHover = useCallback(() => setHoverTarget((cur) => (cur ? null : cur)), []);

  const onPointerDown = useCallback(
    (mode: DragMode) => (e: React.PointerEvent<HTMLElement>) => {
      // No `isDesktopPointer` gate here: "move" must work on touch too
      // (Phase 6, F3a — this box only ever represents the selection, so
      // there's nothing to disambiguate against), and "resize-w" is only
      // ever reachable through the handle below, which is itself desktop-only.
      if (!scale || !rect || !hasDragTarget) return;
      e.stopPropagation();
      e.preventDefault();
      const el = e.currentTarget as HTMLElement;
      el.setPointerCapture(e.pointerId);
      dragRef.current = {
        mode,
        startClientX: e.clientX,
        startClientY: e.clientY,
        origX: rect.x,
        origY: rect.y,
        origW: rect.w,
        origH: rect.h,
        el,
        moved: false,
        axisLock: null,
      };
    },
    [scale, rect, hasDragTarget]
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      if (!d || !scale || !hasDragTarget) return;
      const dxPx = e.clientX - d.startClientX;
      const dyPx = e.clientY - d.startClientY;
      if (!d.moved && Math.hypot(dxPx, dyPx) < DRAG_THRESHOLD_PX) return;
      d.moved = true;

      const dxComp = dxPx / scale;
      const dyComp = dyPx / scale;
      const radiusComp = SNAP_RADIUS_PX / scale;
      const noSnap = e.altKey;

      if (d.mode === "resize-w") {
        let w = Math.max(MIN_WIDTH, d.origW + dxComp);
        const { xs } = getSnapTargets(dragSection, dragIndex);
        w = snap(d.origX + w, xs, radiusComp, noSnap) - d.origX;
        w = Math.max(MIN_WIDTH, w);
        boxRef.current!.style.width = `${w * scale}px`;
        showHud(d.el, `width: ${Math.round(w)}`);
        (d as unknown as { lastW: number }).lastW = w;
        return;
      }

      if (d.mode === "resize-wh") {
        // presenter's own corner handle — both axes grow with the pointer,
        // no snapping (there's nothing meaningful to snap a full inset's
        // size to, unlike a card's left/right edges).
        const w = Math.max(MIN_WIDTH, d.origW + dxComp);
        const h = Math.max(MIN_HEIGHT, d.origH + dyComp);
        boxRef.current!.style.width = `${w * scale}px`;
        boxRef.current!.style.height = `${h * scale}px`;
        showHud(d.el, `${Math.round(w)} × ${Math.round(h)}`);
        (d as unknown as { lastW: number; lastH: number }).lastW = w;
        (d as unknown as { lastW: number; lastH: number }).lastH = h;
        return;
      }

      // Shift constrains to whichever axis has the larger movement, decided
      // once the drag has actually started (not on every move) so a small
      // hand tremor right at the threshold doesn't flip the lock back and forth.
      if (e.shiftKey && !d.axisLock) {
        d.axisLock = Math.abs(dxPx) >= Math.abs(dyPx) ? "x" : "y";
      } else if (!e.shiftKey) {
        d.axisLock = null;
      }

      const { xs, ys } = getSnapTargets(dragSection, dragIndex);
      let x = d.axisLock === "y" ? d.origX : snap(d.origX + dxComp, xs, radiusComp, noSnap);
      let y = d.axisLock === "x" ? d.origY : snap(d.origY + dyComp, ys, radiusComp, noSnap);
      x = Math.max(0, Math.min(x, NATIVE_W));
      y = Math.max(0, Math.min(y, NATIVE_H));

      d.el.style.transform = `translate(${(x - d.origX) * scale}px, ${(y - d.origY) * scale}px)`;
      showHud(d.el, `x: ${Math.round(x)}, y: ${Math.round(y)}`);
      (d as unknown as { lastX: number; lastY: number }).lastX = x;
      (d as unknown as { lastX: number; lastY: number }).lastY = y;
    },
    [scale, dragSection, dragIndex, hasDragTarget, getSnapTargets]
  );

  const finish = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const d = dragRef.current;
      dragRef.current = null;
      hideHud();
      if (!d) return;
      try {
        d.el.releasePointerCapture(e.pointerId);
      } catch {
        /* already released */
      }

      const dd = d as unknown as { lastX?: number; lastY?: number; lastW?: number; lastH?: number };
      const box = boxRef.current;
      if (d.moved && box && scale) {
        // Paint the FINAL committed position/size directly on the box,
        // rather than clearing the in-flight transform/width and waiting
        // for React to supply fresh ones on the next render. Confirmed
        // live: PreviewOverlay reads PreviewPane's DEBOUNCED `props`
        // (150-400ms, load-bearing — it's what keeps a drag here from
        // re-rendering the ~40-component XiaojinEditorial composition on
        // every pointermove), so clearing left a real window where an
        // absolutely-positioned box with `left` set but no `width` shrinks
        // to its content's ~4px natural size — not a data bug (the
        // underlying item.width was always correct), a pure visual flash.
        const finalX = dd.lastX ?? d.origX;
        const finalY = dd.lastY ?? d.origY;
        const finalW = dd.lastW ?? d.origW;
        box.style.transform = "";
        box.style.left = `${finalX * scale}px`;
        box.style.top = `${finalY * scale}px`;
        box.style.width = `${finalW * scale}px`;
        if (d.mode === "resize-wh") {
          box.style.height = `${(dd.lastH ?? d.origH) * scale}px`;
        }
      }

      if (!d.moved) {
        // A tap on the box, not a drag — the box sits on top of everything
        // else in paint order, so without this, tapping an already-selected
        // card's box would silently swallow the click instead of running
        // the same click-again-to-go-deeper cycling a click on bare canvas
        // gets (handleCanvasClick). Resize-handle taps don't cycle — a
        // no-op tap on a 10px handle isn't "clicking the card".
        if (d.mode === "move") handleCanvasClick(e.clientX, e.clientY);
        return;
      }
      if (!hasDragTarget || !item || !dragSection) return;

      // presenter stores its box as x/y/w/h (not x/y/width like every other
      // draggable section) — write to the field names its OWN schema/render
      // component actually reads, matching STATIC_ELEMENTS.presenter.rect's
      // read side.
      const widthField = isTwoAxisResizeSection(dragSection) ? "w" : "width";
      const next = { ...item };
      if (d.mode === "resize-wh") {
        if (dd.lastW != null) next[widthField] = Math.round(dd.lastW);
        if (dd.lastH != null) next.h = Math.round(dd.lastH);
      } else if (d.mode === "resize-w") {
        if (dd.lastW != null) next[widthField] = Math.round(dd.lastW);
      } else {
        if (dd.lastX != null) next.x = Math.round(dd.lastX);
        if (dd.lastY != null) next.y = Math.round(dd.lastY);
      }
      onItemChange(dragSection, dragIndex, next);
    },
    [item, dragSection, dragIndex, hasDragTarget, onItemChange, scale, handleCanvasClick]
  );

  // Root-level click-to-select. Deliberately raw pointerdown/up + a move
  // threshold rather than React's onClick: the selection box (when present)
  // sits on top and handles taps on ITSELF via `finish` above — using
  // native "click" bubbling for the root as well would double-handle a tap
  // on the box (both the box's own handler and a bubbled click on root),
  // since stopPropagation on pointerdown doesn't stop the later click event
  // from bubbling. Pointer down/up on the SAME target with matching
  // identity is exactly what a plain click is, without that ambiguity.
  const rootDownRef = useRef<{ clientX: number; clientY: number } | null>(null);

  const onRootPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    rootDownRef.current = { clientX: e.clientX, clientY: e.clientY };
  }, []);

  const onRootPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const start = rootDownRef.current;
      rootDownRef.current = null;
      if (!start) return;
      if (Math.hypot(e.clientX - start.clientX, e.clientY - start.clientY) > DRAG_THRESHOLD_PX) return;
      handleCanvasClick(e.clientX, e.clientY);
    },
    [handleCanvasClick]
  );

  const onRootPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      // Suppress while a drag is in flight on the box — this handler must
      // never setState mid-drag (see this file's own header comment: a
      // React re-render here is a full XiaojinEditorial composition
      // re-render via the debounced props one level up).
      if (dragRef.current) return;
      handleCanvasPointerMove(e.clientX, e.clientY);
    },
    [handleCanvasPointerMove]
  );

  // Hover outline's own rect — recomputed on every render (cheap: one
  // lookup + one estimatedRect/resolveStaticRect call) so it tracks
  // hoverTarget's CURRENT position/visibility rather than a snapshot taken
  // at hover time; the frame keeps advancing during playback even when the
  // pointer is still. Same draggable-vs-static split as the selection
  // outline below — a static hit's own rect is null for backdrop-kind
  // elements, same "no noise" reasoning.
  // Same drag proxy as the selection: hovering a caption phrase should
  // preview the captionPosition box, not come up empty just because
  // "captions" itself has no x/y.
  const hoverProxySection = hoverTarget ? (DRAG_PROXY[hoverTarget.section] ?? hoverTarget.section) : null;
  const hoverProxyIndex = hoverTarget ? (DRAG_PROXY[hoverTarget.section] ? 0 : hoverTarget.index) : null;
  const hoverDraggable = hoverProxySection != null && hoverProxyIndex !== null && isDraggableSection(hoverProxySection);
  const hoverItem = hoverDraggable ? (itemAt(props, hoverProxySection!, hoverProxyIndex) ?? null) : null;
  const hoverRect = hoverTarget
    ? hoverDraggable
      ? (hoverItem ? estimatedRect(hoverProxySection!, hoverItem) : null)
      : resolveStaticRect(hoverTarget.section, hoverTarget.index)
    : null;
  const hoverVisible =
    !!hoverRect &&
    !!hoverTarget &&
    !sameHitTarget(hoverTarget, section != null ? { section, index } : null) &&
    (hoverDraggable
      ? (() => {
          const range = itemTimeRange(hoverProxySection!, hoverItem!, durationInFrames, cuts);
          return !!range && frame >= range.from && frame < range.to;
        })()
      : resolveStaticVisible(hoverTarget.section, hoverTarget.index));

  const rootHandlers = {
    ref: rootRef,
    onPointerDown: onRootPointerDown,
    onPointerUp: onRootPointerUp,
    onPointerCancel: () => { rootDownRef.current = null; },
    onPointerMove: onRootPointerMove,
    onPointerLeave: clearHover,
  };

  const hoverOutline = hoverVisible && scale ? (
    <div
      className="preview-overlay__hover"
      style={{
        position: "absolute",
        left: hoverRect!.x * scale,
        top: hoverRect!.y * scale,
        width: hoverRect!.w * scale,
        height: hoverRect!.h * scale,
        pointerEvents: "none",
      }}
    />
  ) : null;

  // Non-interactive — no pointer handlers, no resize handle. A visible
  // "you selected this" indicator for static elements that don't (yet, or
  // ever) support dragging; see resolveStaticRect's own comment for why
  // backdrop-kind elements never reach here (their rect resolves to null).
  const staticSelectionOutline = staticSelectedVisible && scale ? (
    <div
      className="preview-overlay__static-selection"
      style={{
        position: "absolute",
        left: staticSelectedRect!.x * scale,
        top: staticSelectedRect!.y * scale,
        width: staticSelectedRect!.w * scale,
        height: staticSelectedRect!.h * scale,
        pointerEvents: "none",
      }}
    />
  ) : null;

  // `hasDragTarget`, not raw `section`/`index` — an object-shaped selection
  // (qrContact, presenter) has index:null, which used to read as "nothing
  // selected" here and silently skip rendering the box even though
  // `visible`/`rect`/`item` were already correctly resolved above.
  if (!scale || !visible || !rect || !hasDragTarget) {
    return (
      <div className="preview-overlay" style={{ position: "absolute", inset: 0, pointerEvents: "auto" }} {...rootHandlers}>
        {hoverOutline}
        {staticSelectionOutline}
        <div ref={hudRef} className="preview-overlay__hud" style={{ display: "none" }} />
      </div>
    );
  }

  return (
    <div className="preview-overlay" style={{ position: "absolute", inset: 0, pointerEvents: "auto" }} {...rootHandlers}>
      {hoverOutline}
      <div
        ref={boxRef}
        className="preview-overlay__box"
        style={{
          position: "absolute",
          left: rect.x * scale,
          top: rect.y * scale,
          width: rect.w * scale,
          height: rect.h * scale,
          pointerEvents: "auto",
        }}
        onPointerDown={onPointerDown("move")}
        onPointerMove={onPointerMove}
        onPointerUp={finish}
        onPointerCancel={finish}
      >
        {isDesktopPointer && dragSection && (
          isTwoAxisResizeSection(dragSection) ? (
            <span
              className="preview-overlay__handle preview-overlay__handle--corner"
              onPointerDown={onPointerDown("resize-wh")}
              onPointerMove={onPointerMove}
              onPointerUp={finish}
              onPointerCancel={finish}
            />
          ) : (
            <span
              className="preview-overlay__handle preview-overlay__handle--w"
              onPointerDown={onPointerDown("resize-w")}
              onPointerMove={onPointerMove}
              onPointerUp={finish}
              onPointerCancel={finish}
            />
          )
        )}
      </div>
      <div ref={hudRef} className="preview-overlay__hud" style={{ display: "none" }} />
    </div>
  );
}
