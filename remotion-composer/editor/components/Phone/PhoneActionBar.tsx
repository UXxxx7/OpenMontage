import React from "react";

export type PhoneSheetKind = string | null;
export type PhoneActionItem = { kind: Exclude<PhoneSheetKind, null>; icon: string; label: string };

/** Arm A's own fixed sheet list — kept as a named export so PhoneShell.tsx's
 *  call site doesn't need to inline it, and so Arm B's shell visibly does
 *  NOT reuse it (a different, smaller item list — see AuthoredPhoneShell.tsx). */
export const ARM_A_SHEET_ITEMS: PhoneActionItem[] = [
  { kind: "add", icon: "✚", label: "Add" },
  { kind: "cut", icon: "✂", label: "Cut" },
  { kind: "edit", icon: "🎛", label: "Edit" },
  { kind: "audio", icon: "🔊", label: "Audio" },
];

/**
 * Bottom icon row (Phase 6, F2b) — the phone shell's answer to AddMenu /
 * CutsPanel / ClipInspector / the Audio section all being simultaneously
 * visible on desktop (there's no room for that on a phone). Genericized
 * (Phase 5 of the Arm B parity work) from a hardcoded 4-sheet list to an
 * `items` prop — Arm B needs a different, smaller set (no Add/Cut/Audio;
 * it can't add elements and has no cuts or a separate audio panel).
 *
 * "Layers" is deliberately NOT a sheet — it toggles Timeline's own existing
 * by-type/layers view toggle in place. Opening a sheet over the timeline to
 * change how the timeline itself is grouped would hide the very thing
 * being changed. Optional here since Arm B's Layers view exists (Phase 2)
 * but is read-only — still worth toggling to inspect stacking order, but a
 * future caller with no layers concept at all can omit it.
 */
export function PhoneActionBar({
  items,
  activeSheet,
  onOpenSheet,
  layersToggle,
}: {
  items: PhoneActionItem[];
  activeSheet: PhoneSheetKind;
  onOpenSheet: (kind: Exclude<PhoneSheetKind, null>) => void;
  layersToggle?: { active: boolean; onToggle: () => void; title?: string };
}) {
  return (
    <div className="phone-actionbar">
      {items.map((it) => (
        <button
          key={it.kind}
          type="button"
          className="phone-actionbar__btn"
          aria-selected={activeSheet === it.kind}
          onClick={() => onOpenSheet(it.kind)}
        >
          <span className="phone-actionbar__icon">{it.icon}</span>
          <span className="phone-actionbar__label">{it.label}</span>
        </button>
      ))}
      {layersToggle && (
        <button
          type="button"
          className="phone-actionbar__btn"
          aria-selected={layersToggle.active}
          onClick={layersToggle.onToggle}
          title={layersToggle.title ?? "Group timeline clips by stacking order"}
        >
          <span className="phone-actionbar__icon">⬚</span>
          <span className="phone-actionbar__label">Layers</span>
        </button>
      )}
    </div>
  );
}
