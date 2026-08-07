// Selection broadcast for the Phase B preview overlay — mirrors
// state/playhead.ts's module-level store pattern, but for a different
// reason: PreviewPane.tsx's own doc comment is explicit that selection
// changes must NOT cause it to re-render (it's React.memo'd specifically so
// a parent state change like "which item is selected" never re-renders the
// whole XiaojinEditorial composition tree it mounts via <Player>). Passing
// `selection` as a normal prop from App.tsx would defeat that memo the
// moment the user clicks a different clip. PreviewOverlay instead
// subscribes to this store directly and keeps its own local state — it is
// NOT wrapped in memo itself and re-rendering IT on selection change is
// exactly what's wanted, it just must never bubble up into PreviewPane.
//
// Unlike playhead's store, this one's listeners MAY call setState — the
// no-setState rule over there exists because frame updates fire up to
// 30x/sec during playback; selection changes maybe a few times a minute.

// index: null selects a top-level OBJECT field (intro/outro/compliance/…),
// not an array item — see state/model.ts's itemAt() for the read side.
export type Selection = { section: string; index: number | null } | null;

type Listener = (selection: Selection) => void;

const listeners = new Set<Listener>();

export const selectionRef: { current: Selection } = { current: null };

export function broadcastSelection(selection: Selection): void {
  selectionRef.current = selection;
  listeners.forEach((l) => l(selection));
}

export function subscribeSelection(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
