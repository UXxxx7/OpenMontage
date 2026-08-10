// Arm B's captions lane — groups the job's `words` (burned-in caption
// timings, already on the client via AuthoredData.words) into short,
// draggable/retimeable/text-editable chunks for the timeline + Inspector.
//
// IMPORTANT — this chunking is an APPROXIMATION, not a re-implementation of
// how any given generated scene actually groups words for burn-in display.
// Three real jobs' scene.tsx files were inspected directly and each groups
// words with a DIFFERENT rule at render time (`len>=4 || gap>12 frames`;
// `len>=4 || dur>=1.6s || gap>0.35s`; a fixed `slice(i,i+4)` with no gap
// logic at all) — there is no stable syntactic anchor to parse the model's
// real grouping out of arbitrary generated TSX, and asking the model to
// additionally emit its own grouping has the same reliability problem
// documented in recoverManifest.ts (a much more emphatic, worked-example
// prompt instruction for x/y/w/h wiring still landed at 0/18 in production).
//
// The fix is to make every EDIT operate on individual `words[]` entries,
// not on chunk boundaries — chunk boundaries are only a display grouping
// for this timeline/Inspector, and are exactly reproducible from the
// (possibly-edited) words array at any time via chunkWords(). Whatever the
// real render's own grouping does with the edited words is a presentation
// detail; the words themselves (text + timing) are what's authoritative and
// exact.

export type Word = { word: string; start: number; end: number };

export type CaptionChunk = {
  /** Index into the words array of this chunk's FIRST word — the stable
   *  identity for this chunk across re-chunks. Chunk array position is NOT
   *  stable (a text edit that changes word count re-derives chunk
   *  boundaries from scratch), but the edited chunk's first word always
   *  lands back at the same words-array index it started at (splices are
   *  in place), so re-deriving chunks after an edit and finding the one
   *  whose startIndex still equals this key correctly re-identifies "the
   *  chunk the user was just working on." */
  key: number;
  startIndex: number;
  /** Inclusive. */
  endIndex: number;
  text: string;
  fromSec: number;
  toSec: number;
};

/** A chunk closes once it reaches this many words. */
const CHUNK_MAX_WORDS = 4;
/** A chunk also closes when the gap to the next word exceeds this — and
 *  (see clampChunkFromSec) a chunk's start may never be dragged closer than
 *  this to the previous chunk's end, or the two would silently re-merge
 *  under this exact same rule the next time chunkWords() runs. Same
 *  threshold both directions on purpose — using two different numbers here
 *  would make the clamp only approximately prevent the merge it exists to
 *  prevent. */
const CHUNK_GAP_SEC = 0.35;

export function chunkWords(words: Word[]): CaptionChunk[] {
  if (words.length === 0) return [];
  const chunks: CaptionChunk[] = [];
  let start = 0;
  for (let i = 1; i <= words.length; i++) {
    const atEnd = i === words.length;
    const tooLong = i - start >= CHUNK_MAX_WORDS;
    const bigGap = !atEnd && words[i].start - words[i - 1].end > CHUNK_GAP_SEC;
    if (atEnd || tooLong || bigGap) {
      const slice = words.slice(start, i);
      chunks.push({
        key: start,
        startIndex: start,
        endIndex: i - 1,
        text: slice.map((w) => w.word).join("").trim(),
        fromSec: slice[0].start,
        toSec: slice[slice.length - 1].end,
      });
      start = i;
    }
  }
  return chunks;
}

export function findChunkByKey(chunks: CaptionChunk[], key: number): CaptionChunk | undefined {
  return chunks.find((c) => c.key === key);
}

/** After a text edit that changed word count, the edited chunk's `key`
 *  (its ORIGINAL startIndex) may no longer be any chunk's exact start —
 *  find whichever chunk now contains that words-array index instead. */
export function findChunkContainingIndex(chunks: CaptionChunk[], index: number): CaptionChunk | undefined {
  return chunks.find((c) => c.startIndex <= index && index <= c.endIndex);
}

/** A chunk's start may never be dragged within CHUNK_GAP_SEC of the
 *  previous chunk's end — chunkWords() would silently re-merge the two on
 *  the very next re-chunk (e.g. right after Save re-renders and this
 *  timeline recomputes from the new words), so the timeline would show two
 *  chunks while the render only has one. */
export function clampChunkFromSec(chunks: CaptionChunk[], key: number, proposedFromSec: number): number {
  const idx = chunks.findIndex((c) => c.key === key);
  if (idx <= 0) return proposedFromSec;
  const prevEnd = chunks[idx - 1].toSec;
  return Math.max(proposedFromSec, prevEnd + CHUNK_GAP_SEC);
}

const MIN_WORD_DURATION_SEC = 1 / 30; // floor for a degenerate (zero-length) word span; overwritten below if a real fps is known

/** Retime a chunk by an affine remap of its member words' start/end from
 *  the chunk's OLD [fromSec,toSec] span into a NEW one — handles a pure
 *  shift (move — newSpan == oldSpan, degenerates to translate-by-delta) and
 *  a resize (either edge changes) with the same formula, matching how
 *  useClipDrag already reports {from, to} per drag mode. Words OUTSIDE the
 *  chunk are never touched, EXCEPT that the requested [newFromSec,newToSec]
 *  is first clamped against the immediately adjacent words (one before
 *  startIndex, one after endIndex) — confirmed by direct testing against a
 *  real job's words array: without this clamp, moving/resizing a chunk can
 *  push it past its neighbor's boundary and invert the flat words array's
 *  ordering, which is a correctness bug (chunkWords()/the real render both
 *  assume strictly increasing start/end), not just a cosmetic overlap the
 *  way two overlapping clips in an Arm A lane would be. `clampChunkFromSec`
 *  additionally holds callers to a wider (0.35s) buffer against the
 *  previous CHUNK specifically to stop chunkWords() from re-merging the two
 *  on the next recompute — a UX concern, not a correctness one — so callers
 *  should still call it before this function; this clamp is the
 *  correctness backstop regardless of whether they did. */
export function retimeChunkWords(
  words: Word[],
  chunk: CaptionChunk,
  newFromSec: number,
  newToSec: number,
  fps = 30
): Word[] {
  const minDur = 1 / fps;
  const prevWord = chunk.startIndex > 0 ? words[chunk.startIndex - 1] : null;
  const nextWord = chunk.endIndex < words.length - 1 ? words[chunk.endIndex + 1] : null;
  const minFrom = prevWord ? prevWord.end + minDur : 0;
  const maxTo = nextWord ? nextWord.start - minDur : Infinity;

  let clampedFrom = Math.max(newFromSec, minFrom);
  let clampedTo = Math.min(newToSec, maxTo);
  if (clampedTo <= clampedFrom) {
    // Neighbors already leave no room to move at all — collapse to a
    // minimal-width no-op at the nearest legal point instead of inverting.
    clampedFrom = Number.isFinite(maxTo) ? Math.min(clampedFrom, maxTo - minDur) : clampedFrom;
    clampedTo = clampedFrom + minDur;
  }

  const oldFrom = chunk.fromSec;
  const oldSpan = Math.max(chunk.toSec - oldFrom, minDur);
  const newSpan = Math.max(clampedTo - clampedFrom, minDur);
  const scale = newSpan / oldSpan;

  const out = words.slice();
  let prevEnd = clampedFrom;
  for (let i = chunk.startIndex; i <= chunk.endIndex; i++) {
    const w = words[i];
    let start = clampedFrom + (w.start - oldFrom) * scale;
    let end = clampedFrom + (w.end - oldFrom) * scale;
    // Monotonicity guard — floating-point remap of already-tight word
    // boundaries can otherwise invert a pair under extreme compression.
    start = Math.max(start, prevEnd);
    end = Math.max(end, start + minDur);
    out[i] = { ...w, start, end };
    prevEnd = end;
  }
  // Snap the chunk's own outer edges back to the exact clamped values —
  // the per-word floor above can drift the last word's end forward on
  // extreme compression; keeping the chunk's displayed span (Ruler/Playhead
  // read chunk.toSec, not the last word's own end) in sync with the last
  // word avoids a confusing one-frame mismatch next time this chunk is
  // re-derived.
  const lastIdx = chunk.endIndex;
  if (out[lastIdx].end < clampedTo) out[lastIdx] = { ...out[lastIdx], end: clampedTo };
  return out;
}

/**
 * Apply an edited chunk text back onto the words array. Returns null when
 * the edit should be REJECTED (empty text) — caller keeps the prior value.
 *
 * O = original tokens (this chunk's current words). T = the new text split
 * on whitespace.
 *   |T| == |O|  → 1:1 rename. Timings untouched — this is the overwhelmingly
 *                 common case (fixing a mis-transcribed word) and it's
 *                 lossless.
 *   |T| != |O|  → hold the chunk's OUTER span fixed ([firstWord.start,
 *                 lastWord.end]), redistribute proportionally by
 *                 (token length + 1) so longer words claim proportionally
 *                 more of the span, splice the new word entries in place.
 *                 First start / last end are snapped back to the exact
 *                 originals afterward (floating-point drift guard).
 */
export function applyChunkTextEdit(words: Word[], chunk: CaptionChunk, newText: string): Word[] | null {
  const original = words.slice(chunk.startIndex, chunk.endIndex + 1);
  const newTokens = newText.trim().split(/\s+/).filter(Boolean);
  if (newTokens.length === 0) return null;

  const out = words.slice();

  if (newTokens.length === original.length) {
    for (let i = 0; i < original.length; i++) {
      const pad = original[i].word.startsWith(" ") ? " " : "";
      out[chunk.startIndex + i] = { ...original[i], word: pad + newTokens[i] };
    }
    return out;
  }

  const outerFrom = original[0].start;
  const outerTo = original[original.length - 1].end;
  const totalSpan = Math.max(outerTo - outerFrom, MIN_WORD_DURATION_SEC);
  const weights = newTokens.map((t) => t.length + 1);
  const totalWeight = weights.reduce((a, b) => a + b, 0);

  const replacement: Word[] = [];
  let cursor = outerFrom;
  for (let i = 0; i < newTokens.length; i++) {
    const dur = (weights[i] / totalWeight) * totalSpan;
    const start = cursor;
    const end = i === newTokens.length - 1 ? outerTo : cursor + dur;
    replacement.push({ word: " " + newTokens[i], start, end });
    cursor = end;
  }
  replacement[0].start = outerFrom;
  replacement[replacement.length - 1].end = outerTo;

  out.splice(chunk.startIndex, original.length, ...replacement);
  return out;
}

/** Staleness stamp for the `__captions` override — cheap fingerprint of the
 *  words array it was computed against, NOT a hash of full content (a full
 *  hash would need to run over every real render too; this only needs to
 *  catch "the job's transcript was regenerated since this edit was made",
 *  which changes length and/or the first/last word's timing in practice). */
export function wordsBaseStamp(words: Word[]): string {
  if (words.length === 0) return "0";
  return `${words.length}:${words[0].start}:${words[words.length - 1].end}`;
}
