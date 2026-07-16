# whatsapp_mvp — read this before touching content_planner.py / apply_style

This file exists because a real bug shipped (job_88b957f807b9 / job_0aaef74e8865,
"MrBeast" video) that a five-minute reread of this file would have caught. Read it
before changing `content_planner.py`, `pipeline_runner.py`'s apply_style path, or
anything that touches `sections`/`workflow_ranges`/takeovers.

## Rule 1 — test with more than one content shape before calling a fix done

Every fix this project has shipped was validated against **David's insurance-renewal
video** (short discrete facts: a date, two dollar amounts, a risk statement) because
that's the one fixture on hand. That video's shape hides entire classes of bugs:

- It never has a `process_timeline` (no multi-stage narrative).
- Its takeovers are always short relative to the video (gauge/warning sections),
  so the 30% hidden-budget cap (`_HIDDEN_BUDGET_FRACTION`, D2) rarely binds hard
  enough to matter.
- Its content is dense throughout, so a demoted/removed section never leaves a
  visible dead zone — something else is usually nearby to fill the gap.

A **long-narrative** video (a single 15-30s explanation of a multi-step process,
e.g. "idea → production → filming → editing", a MrBeast-style cost/process
breakdown) exercises a completely different code path: `_plan_process_timeline`,
and it stresses D2 in a way David's video structurally cannot (a timeline
legitimately wants to hide the speaker for most of its own runtime). **Before
saying a content_planner/apply_style change is verified, run it against at least
one video that isn't David's** — ask the user for one, or reuse one of the job
inputs already sitting in `storage/jobs/*/input.mp4` if a suitable one exists.

## Rule 2 — `process_timeline`'s content is NOT independent of its section

Every other visual type (`gauges`, `data_cards`, `countdowns`, `calendar_events`,
`topic_cards`, ...) lives in its own top-level list in the frame plan, independent
of the `sections`/takeover it happens to render inside. When D1/D2 cap or remove a
takeover section, those other visuals are untouched — they just render in normal
workflow mode instead of full-canvas.

`process_timeline` is the one exception: its entire visual payload
(`sec["timeline"] = {"heading": ..., "nodes": [...]}`) is stored **on the section
object itself**, produced by `_plan_process_timeline` and attached in `_to_frame_plan`
around the takeover-building loop (~line 1042-1054). There is no independent
`timelines` list to fall back on.

**Confirmed bug (fixed 2026-07-15):** D2's demotion loop
(`_to_frame_plan`, the `while sections and duration_frames > 0:` block) used to do
`sections.remove(longest)` unconditionally whenever a takeover exceeded the 30%
hidden-budget cap. For a video where the process_timeline's own chapter spans most
of the runtime (confirmed real case: 77% of a 22.8s video), this deleted the
**entire timeline** — every stage, every number — leaving nothing but captions for
that whole stretch. The existing code comment justifying the demotion ("missing a
decorative title doesn't affect the content itself displaying normally") is true for
every visual type *except* this one.

**The fix**: when the section being demoted has a `"timeline"` key, cap its
`toFrame` down to `fromFrame + _TAKEOVER_HARD_CAP_FRAMES` (the same 8s hard cap
non-timeline takeovers already get from D1) instead of removing it — the timeline's
nodes reveal progressively via their own `revealFrame`, so a capped section still
shows however many stages naturally landed within that window. Only fall through to
full removal if capping alone still doesn't bring the total under budget (rare —
would mean the video has multiple oversized timeline sections).

**If you touch D1/D2 again**: any change to takeover capping/demotion must ask
"does this section's visual content live independently of the section object, or is
it baked into the section like `timeline` is?" before choosing remove-vs-cap.
`quote` gets the opposite treatment already (never demoted — see the D2 comment) for
a related reason (its content is also section-bound, via the sentinel workflow
range).

## Rule 3 — the plan-quality criterion loop is the gate; extend IT, not one-off patches

`plan_content` runs a **criterion loop** (user-mandated, 2026-07-15): up to
`_PLAN_MAX_ATTEMPTS` (3) LLM planning rounds; after each round the plan is scored
by `_plan_quality_failures` (pure function, deterministic, NO LLM self-review).
All criteria pass → return early. Failures → fed verbatim into the next round's
prompt. Rounds exhausted → deliver the round with the fewest failures (best-of),
with zero-display cards hard-dropped before delivery.

Current criteria (each maps to a confirmed production failure):
1. **Zero visuals** on a ≥15s video (Dickson video: 35s, 0 graphics, subtitles only).
2. **Uncovered span > 8s** mid-video (`_sparse_gaps`).
3. **Zero-display count_up cards** ("$0.0M" after divideBy/decimals).

**If a new class of bad plan ships, the fix is a new criterion in
`_plan_quality_failures`** — not a new standalone retry block. The old
architecture (separate one-shot retries for zero-values and richness) is gone;
`_apply_richness_floor` survives only for its unit tests.

The preview WhatsApp message now names the actual animations (GET /jobs/{id}
`animations` field, derived from the rendered props file by
`webhook._animations_summary`; rendered into the message by
`server/worker.js previewReadyMessage`). If you add a new visual type, add it to
`_animations_summary` or it will silently not be announced.

## Rule 4 — a full-canvas takeover must actually fill the canvas; a title alone is not content

Confirmed real bug (2026-07-16, dajaai-walking-fresh backtest, job_2dd37e39dfa6 — user
sent a screenshot): a "流程/PROCESS" takeover section with no `timeline` and no `icon`
renders as a section title + a purely decorative gradient blob (`SectionLayer.tsx`'s
fallback branch, y=500-1000) — the speaker is fully hidden (`opacityKeyframes` per
Fix D3), but nothing fills the space they vacated. The section's other content
(`topicCards` in this case) mounts using its normal content-zone coordinates, unaware
it's inside a takeover, so it renders far below the empty icon zone. User's words:
*"still so many empty spaces, might as well just park the facecam there instead of
removing for a mere title."*

**Root cause, one layer deeper than it looks:** `TimelineSection.tsx` — the actual
rich graphic (growing progress line, spring-scaled dots, counting numbers) that
*should* fill this space — was hardcoded to dark-canvas colors, so `SectionLayer.tsx`
only rendered it `{s.timeline && mode === "dark" ? ... }`, and `content_planner.py`'s
`_plan_process_timeline` **force-overrode every process_timeline chapter's `dark` to
`true`** specifically because of that hardcoding. Net effect: the richest, most
"advanced" graphic in the whole component library was structurally locked out of
`colorMode: "warm"` — the *default* mode most jobs actually use — so it could never
be offered for a normal-tone video, only for warning/dramatic ones.

**The fix (Fix D6):** `TimelineSection.tsx` now reads `theme.ts`'s `PALETTES[colorMode]`
like every other component instead of hardcoding dark values; `SectionLayer.tsx`
renders it whenever `s.timeline` is set, regardless of mode; `content_planner.py` no
longer force-sets `_dark = True` — a process_timeline chapter's dark/light styling
now follows the content's actual tone, same as every other chapter. Verified with a
direct render: the same content that produced the empty screenshot now shows the full
animated timeline correctly on a cream background.

**New permanent guard:** `props_lint.py`'s `section_takeover_lacks_content` check
flags any takeover section with neither `timeline` nor `icon` — this is the exact
geometric signature of the bug above, computable without rendering. Its finding text
feeds back into the props_lint retry loop automatically (same mechanism as every
other check), so a future regression gets caught and retried, not shipped silently.

**If you touch `SectionLayer`/`TimelineSection` again:** don't reintroduce a
mode-gated graphic. If a new full-canvas visual only works in one color mode, that's
the bug to fix (make it palette-aware), not a constraint to route around by forcing
chapters into that mode.

## Rule 5 — a criterion-loop candidate that "wins" must be written back everywhere the next stage reads from, not just held in a variable

Confirmed real bug (2026-07-16, found while verifying Rule 4 against the same
backtest): the Fix C5/C6 `props_lint` retry loop in `pipeline_runner.py`'s
`_op_apply_style` tracks the winning candidate in an in-memory `best_props` variable
— but `_build()` **unconditionally writes `props_path` to disk on every call**,
including calls whose result loses the comparison. When the winning candidate isn't
the *last* `_build()` call in the loop (i.e. the loop ran a further attempt that
didn't improve on the winner), the file on disk was left holding the losing
attempt's content, not `best_props` — and the actual `npx remotion render` command
reads `--props=<props_path>` **from disk**, not from the Python variable. The
richness/findings comparison was computing the right answer and then silently not
using it. This didn't surface on the MrBeast backtest only because the winning
attempt there happened to be the loop's last call (the loop exits early once
`best_findings` is empty) — the dajaai backtest's loop ran a non-winning 3rd attempt
after the winner, exposing the desync.

**The fix (Fix C9):** after the loop, explicitly `props_path.write_text(...)` with
`best_props`'s content — don't rely on some `_build()` call's internal write having
coincidentally landed on the right one.

**General lesson, not just for this loop:** whenever a bounded retry/criterion loop
picks a winner from among several candidates that each have their own side effects
(here: writing a file), the side effects of the *losing* candidates aren't
automatically undone — the loop must explicitly re-apply the winner's side effects
after deciding, not assume "the winner already wrote itself correctly" just because
it did at some earlier point in the loop.

## Other non-obvious constraints (running list — add to this, don't let it go stale)

- **Zero-value count_up rows can slip through even after the retry.** The
  `_zero_value_titles` check + one corrective LLM retry (`plan_content`) now always
  runs regardless of whether this is a fresh plan or a props_lint-triggered
  re-plan — but the retry is still just one more LLM call, not a guarantee. A
  dropped card (rather than a wrong one like "$0.0M") is the intended fallback, not
  a bug, when the LLM gets it wrong twice in a row.
- **The mechanical "verbatim transcript → card" gap-filler is gone on purpose.**
  `_fallback_topic_cards_for_gaps` was removed — its content was always identical to
  the caption already on screen ("popups for the sake of having them"). Gaps that
  survive the LLM's own replan attempt are now accepted as-is (speaker + captions,
  no card), not machine-stuffed. Don't reintroduce a text-only fallback without
  solving the "it just repeats the subtitle" problem first.
- **`_gap_fill: True` on a data point forces a fresh stack** (never silently joins
  whatever came before it) — this exists because a gap-filler joining an unrelated,
  already-open stack lets the earlier item's exit get glued to the gap-filler's much
  later timing. Any new gap-filling mechanism must set this flag.
- **Stack handoff prefers holding the outgoing item to its own grounded end**, only
  delaying the incoming item's mount (capped at `_STACK_HANDOFF_MAX_DELAY_FRAMES`,
  6s) rather than truncating the outgoing item early — this only applies when both
  ends are keyword/end_keyword-grounded (Fix E); don't assume it fires for
  fixed-duration fallback timings.
- **Quote is the only "solo" (full-canvas) visual type** and cannot mount within the
  first `_QUOTE_MIN_START_FRAMES` (7s) of the video regardless of whether an intro
  title card exists — this floor is unconditional, not gated behind `if intro:`.

## Where the rest of the story lives

- `docs/STATUS.md` — architecture baseline, branch status (predates most of the
  fixes above — treat as historical, not current).
- `docs/CHANGELOG.md` — dated entries.
- `contracts/README.md` — the frozen op_registry/render_props/style_params shapes.
