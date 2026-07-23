# Changelog (team-maintained)

> **Add an entry for every change** (newest on top). Read alongside `docs/STATUS.md` (current-state snapshot).
>
> ### How to add an entry (copy the template)
> ```
> ## YYYY-MM-DD — one-line title
> - **Who**: P1 / P2 / P3
> - **Branch/commit**: <branch> / <commit sha or PR>
> - **What changed**: which files / capabilities
> - **Why / impact**: what it fixes; what it means for others (restart? re-merge? contract change?)
> - **Status**: ✅ merged to trunk / 🧪 to verify / ⏸ blocked (say on whom)
> ```
> Conventions: if you change a **contract**, mark "⚠️ contract change — needs 3-way sync". If you touch the **planner**, read `docs/L1.5-vs-L2.md` first (L2 only, don't touch L1.5).

---

## 2026-07-23 (2) — "at least one card" and "no invented numbers" don't guarantee "every spoken number got a card" (C51/C52)
- **Who**: Claude
- **Branch/commit**: worktree-whatsapp-message-fix (uncommitted at time of writing)
- **What changed**: two new `_plan_quality_failures` criteria in `content_planner.py`.
  `_uncovered_spoken_values` (criterion 6) flags a spoken monetary figure with no
  matching count_up/before_after value anywhere in the plan — the mirror image of
  C45's `_ungrounded_count_up_rows`. `_spoken_countdown_and_date_together` (criterion 7)
  flags a calendar-only plan when the transcript names both a countdown-style
  day-count and a calendar month in the same ASR segment.
- **Why / impact**: user reported a genuine WhatsApp-delivered preview of the David/
  Pacific Life fixture missing the Coverage ($1.5M) card, the renewal countdown, and
  the outro, worried it was a regression of a previously-fixed bug (possibly from a
  collaborator's in-progress filler-removal work). Direct transcription + frame
  inspection of the actual delivered video showed the cutting was clean (the known
  duplicate retake was correctly collapsed to one instance) — the real cause was
  content_planner dropping the Coverage figure and the countdown while keeping the
  Premium figure and calendar from the very same sentence. Neither C41 ("at least one
  numeric card exists") nor C45 ("no card's number is invented") covers "some numbers
  present, others silently dropped" — a real gap in the existing criteria, not a
  regression. See `whatsapp_mvp/CLAUDE.md` Rule 23.
- **Status**: 🧪 to verify. Both criteria validated at the unit level against REAL
  segment data pulled from `storage/jobs/job_452ef6c48100/_op_nofiller_transcript.json`
  (confirms "30 days" and "the 28th of July" land in the same ASR segment despite
  being split across captions), correctly silent when the plan is complete or the
  transcript is unrelated. Full 6-suite test run clean. Outro's absence not yet
  root-caused (couldn't access the exact job that produced the reported preview to
  inspect its props directly) — flagged as an open question, not guessed at.

---

## 2026-07-23 — worker.js C-roll preview message had a shifted argument, plus 6 new xiaojin cards wired into content_planner
- **Who**: Claude
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**: two independent pieces of work.
  1. **Bug fix**: `server/worker.js`'s `crollGenerate` (the photo→HeyGen-digital-human→video
     flow) called `previewReadyMessage(jobLang, jobId, status.degraded_operations,
     status.generation_cost_usd)` — missing the `status.animations` argument every
     other call site (`editVideo`, `confirmJob`, `retryJob`, the multi/b-roll flow)
     passes. This shifted every later parameter: `degraded_operations` got read as
     `animations` (printing raw op keys like "apply_style" as fake animation names),
     `generation_cost_usd` got read as `degradedOps` (silently dropping the "this step
     failed, reply retry" warning — the exact guarantee Rule 9/C10 built), and the real
     AI-generation cost never got reported. Fixed by adding the missing argument.
  2. **Feature**: the 6 new xiaojin content-zone components sitting unused since the
     last session (`ComparisonCard`/`RankedListCard`/`ChecklistCard`/`LocationPinCard`/
     `TestimonialCard`/`IconClusterCard`, previously only exercised via
     `NewGraphicsDemo.tsx`) are now real content_planner visual types (`comparison`,
     `ranked_list`, `checklist`, `location_pin`, `testimonial`, `icon_cluster`) —
     SYSTEM_PROMPT vocabulary + 6 new `_plan_*` functions in `content_planner.py`,
     wired into every touchpoint `_to_frame_plan` needs (dispatch table, `_est_height`,
     `_dp_seconds` for checklist's per-item timing, the defensive-cleanup/takeover-span/
     same-slot/`_resolve_same_slot_overlaps`/`final_workflow_ranges` loops, the final
     return dict, `_plan_quality_failures`'s `total_visuals`, and `_coverage_spans` —
     this last one is exactly the function Fix C30 already had to patch once for
     step_list/corner_card, see `whatsapp_mvp/CLAUDE.md` Rule 3's own warning), then
     through `pipeline_runner.py` (`_WORKFLOW_CONTENT_PROP_KEYS`, `_RICHNESS_FIELDS`,
     both `op.get`/`content_plan.get` extraction branches, props assembly, the
     mount-floor/dominant-window-avoidance/outro loops), `props_lint.py`
     (`_RICHNESS_FIELDS`, `_EST_HEIGHT`, `_collect_elements`), `webhook.py`
     (`_animations_summary` — required per Rule 3, "add it or it silently won't be
     announced"), `contracts/render_props.schema.json` (mandatory — top-level
     `additionalProperties: false` means an unlisted prop key hard-fails ajv
     validation, not a silent skip), and `XiaojinEditorial.tsx` (imports, prop types,
     interface fields, destructure, render blocks).
- **Why / impact**: (1) is a real production bug — every C-roll job's preview message
  has been showing garbled/missing content since whichever commit introduced the
  argument-count drift between call sites. (2) unblocks the two new visual types from
  ever being planned — content_planner had no way to select them and the render
  pipeline had no way to render them even if it had.
- **Status**: ✅ verified at the unit/type level — full 6-suite Python test run
  (`test_content_planner`/`test_pipeline_runner`/`test_props_lint`/`test_qa_stills`/
  `test_filler_review`/`test_golden_extraction`) clean, including new regression tests
  per new visual type (happy-path mapping, required-field-missing skip,
  `_coverage_spans`/`total_visuals` wiring — the exact class of bug Rule 3/C30 warns
  about); `npx tsc --noEmit` clean on `remotion-composer`. **Not yet verified via a
  live render or a real WhatsApp job** — per Rule 1/Rule 13's own standard, a
  props-level and type-level pass is necessary but not sufficient; the next step is a
  live `apply_style` run against a video whose transcript actually contains
  comparison/ranked-list/checklist/location/testimonial/icon-cluster-shaped content,
  since none of the existing fixtures (David's insurance video) naturally produce any
  of these 6 new visual types.

---

## 2026-07-21 (9) — C47/C49's avoidance check ran before, not after, the function that actually finalizes scenes (C50)
- **Who**: Claude (same session, next live render after C49)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**: `pipeline_runner.py`'s `_recompute_scenes_from_content`
  now runs `_shift_off_dominant_windows`/`_shift_off_dominant_windows_
  headers` internally against the schedule it just derived, then rebuilds
  `mode_schedule`/`scenes` a second time from the (possibly-adjusted)
  content ranges before returning — folding C47/C49's guarantee into the
  single function that's already the authoritative source of truth for
  `scenes`, instead of relying on callers to re-run it at the right time.
- **Why / impact**: the exact C47/C49 collision reproduced again (ghosted
  "DETAILS" header over the speaker) even with both fixes in place — because
  they ran inside `_build()` *before* its final
  `_recompute_scenes_from_content` call, and that function's own docstring
  (Fix C33/C38) already says any code moving content-zone graphics after it
  runs must call it again. C47/C49 themselves move graphics but ran before,
  not after — Rule 15's lesson recurring a third time. See
  `whatsapp_mvp/CLAUDE.md` Rule 22.
- **Status**: 🧪 to verify. New regression test calls the function directly
  and confirms the returned header is capped. Full 5-suite run clean. Sixth
  live verification render in progress.

---

## 2026-07-21 (8) — C47's fix was scoped to zoneHeaders; the sibling dataCard/countdown/gauge path had the same gap (C49)
- **Who**: Claude (same session, next live verification render after C47+C48)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**: `pipeline_runner.py` — `_shift_off_dominant_windows`
  (the dataCard/gauge/countdown sibling of C47's header fix) now also caps
  `endFrame` via `_next_dominant_grow_start`, same as C47 did for
  `toFrame`. Updated `test_shift_off_dominant_windows_no_op_when_already_
  workflow`'s fixture (it had the exact same latent collision, just never
  checked) and added a dedicated capping test.
- **Why / impact**: a "Renew in 30 Days" card overlapped the speaker
  mid-transition — same mechanism as C47, reproducing in the sibling
  function C47's own writeup had already flagged as an unfixed gap. See
  `whatsapp_mvp/CLAUDE.md` Rule 21.
- **Status**: 🧪 to verify. Full 5-suite test run clean. Live verification
  in progress.

---

## 2026-07-21 (7) — C46 fixed one sampling rule's bad-frame case, not the class of bug (C48)
- **Who**: Claude (same session, immediately after C47 — next live
  verification render, before C47 had even been confirmed, hit yet another
  distinct issue)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**: `qa_stills.py`'s `pick_qa_frames` — added a final pass
  (before the existing frame-0 guard) that pushes any sampled frame landing
  inside ANY count_up row's own `[mountFrame+mountOffset, +45)` animation
  window out to that window's end, regardless of which rule produced it.
- **Why / impact**: `$4,200` flagged against a `$8,400` transcript — exactly
  50% (frame 20/40 of the count-up), not the 75% case C46 fixed. C46 only
  patched the ONE rule that samples data cards directly; this run's mismatch
  came from a *different* rule (transition-window sampling) independently
  landing inside the same animation window. Same failure shape Fix C22
  already solved once (Rule 14: fix the invariant once at the function's
  exit point, not per-rule) — C46 quietly repeated the mistake C22 had
  already named. See `whatsapp_mvp/CLAUDE.md` Rule 20.
- **Status**: 🧪 to verify. New regression test in `test_qa_stills.py`
  (a transition window deliberately landing inside a count_up window,
  independent of C46's own rule) passes; full 5-suite run clean. A fourth
  live verification render is in progress to confirm no further distinct
  issues surface.

---

## 2026-07-21 (6) — zoneHeader dominant-window avoidance only checked the start frame, not the whole window (C47)
- **Who**: Claude (same session, found in the very next live verification
  render after C46 — user asked to keep fixing since "the server is still
  being used")
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**: `pipeline_runner.py` — new `_next_dominant_grow_start`
  helper; `_shift_off_dominant_windows_headers` now also caps a zoneHeader's
  `toFrame` if the SpeakerCard starts regrowing to Dominant size before the
  header's own window ends, not just checking the mode at `fromFrame`.
- **Why / impact**: opened the actual flagged QA still (`f584.png`, per Rule
  9) from job_452ef6c48100's latest run and saw a real, reproducible defect
  — the "COVERAGE" zoneHeader title ghosted over the speaker's face as the
  card grew back to Dominant size mid-window. Root cause: Fix C24's
  avoidance check only inspects the header's `fromFrame` mode — docked at
  381, but the card starts regrowing at 572 (592 minus the 20-frame
  transition), which falls *inside* the header's own 381→592 display span.
  "Starts docked" and "stays docked for the whole window" are different
  claims that C24 conflated. See `whatsapp_mvp/CLAUDE.md` Rule 19.
- **Status**: 🧪 to verify. New regression tests in `test_pipeline_runner.py`
  using the real job's mode_schedule shape pass; full 5-suite test run
  (`test_pipeline_runner`/`test_content_planner`/`test_qa_stills`/
  `test_filler_review`/`test_golden_extraction`) clean. Live re-render to
  confirm this exact frame no longer reproduces is the next step.

---

## 2026-07-21 (5) — vision-QA sampled count_up cards mid-animation, misread as wrong content (C46)
- **Who**: Claude (same session, immediately after C45 — a fresh end-to-end
  re-render of `job_452ef6c48100` with C45 in place still degraded to
  no-video-delivered, so kept digging instead of declaring it fixed)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**: `qa_stills.py`'s `pick_qa_frames` — data-card sample
  point moved from `mountFrame + last_row_mountOffset + 30` to `+ 45`.
- **Why / impact**: inspected the actually-delivered props directly and
  confirmed the planned values were correct (`$8,400`/`$1.5M`, matching the
  transcript exactly) — the vision-QA "mismatch" was reading the count_up
  number's own reveal animation (`InfoCard.tsx`:
  `interpolate(rowLocal, [0, 40], [0, row.value], ...)`, 40 frames to reach
  the final value) 10 frames before it finished. Math checks out exactly:
  `8400 * (30/40) = 6300`, `1,500,000 * (30/40) = 1,125,000` ("$1.1M") — the
  precise two "wrong" values flagged on real runs. Not a content_planner bug
  at all; a QA sampling-timing bug, same class as Rule 14 (frame 0 sampled
  before anything's rendered) but a different mechanism (a component's
  reveal animation legitimately still in progress when sampled). See
  `whatsapp_mvp/CLAUDE.md` Rule 18.
- **Status**: 🧪 to verify. `test_qa_stills.py` covers it with the real
  job's actual props shape (new regression test). Not yet verified via a
  full live re-render — that's the next step.

---

## 2026-07-21 (4) — count_up values weren't checked against the transcript at all (C45)
- **Who**: Claude (same session, next day's live re-run of the C43/C44 fix —
  user's reaction: "you're saying it's error again???? wtf i thought we
  fixed it". Root-caused from real transcript evidence, per standing
  instruction not to guess, before writing any code.)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**: `content_planner.py` — new 5th `_plan_quality_failures`
  criterion, `_ungrounded_count_up_rows`: flags any count_up row whose value
  matches none of the numbers actually spoken in the transcript (digit-form
  via existing `_spoken_dollar_amounts`, plus new `_spoken_word_numbers` for
  word-form amounts like "one and a half million"/"one point five million").
  Tolerance check accounts for the value/divideBy convention ambiguity
  already documented (Rule 15 note, `_zero_value_titles`) — compares against
  each spoken candidate at raw scale and at /1,000 and /1,000,000.
- **Why / impact**: a fresh `run_full_fixed_pipeline.py` re-run against
  `job_452ef6c48100` (the same job Rule 16's C43/C44 writeup investigated
  yesterday and concluded had no reproducible delivered-content bug) hit
  the exact `$6,300`/`$8,400` mismatch again, then a second one
  (`$1.1M`/"one and a half million"), and this time the vision-QA retry did
  NOT self-correct — it degraded to a hard failure with no video delivered
  at all. Confirmed via both the raw and filler-removed transcripts that
  `$6,300`/`$1.1M` never appear anywhere in the source: the LLM is
  inventing plausible-looking wrong figures on some replan rounds, not
  reading a stray retake. The existing C41 criterion only checks that *a*
  numeric card exists for a spoken amount — never that its value is the
  value that was actually spoken — so a hallucinated card sailed through it
  every time. See `whatsapp_mvp/CLAUDE.md` Rule 17 for the full writeup and
  the correction to Rule 16's now-disproven conclusion.
- **Status**: 🧪 to verify. Unit-level verified (reproduces catching both
  hallucinated values against the real job transcript; passes clean on the
  real correct values). Full `test_content_planner.py` /
  `test_golden_extraction.py` / `test_filler_review.py` suites re-run clean
  — including a false positive this change itself introduced and then fixed
  (word-number parser initially mis-split "one point five million" into an
  unrelated "five million"; fixed by handling spoken decimal points). Not
  yet verified via a full live `apply_style` re-render of this job — that's
  the next step before calling this actually fixed, not just detected.

---

## 2026-07-21 (3) — Self-intro-repeat identity bug (C43) + dead deterministic dup-cut backstop (C44)
- **Who**: Claude (same session — user's most urgent pushback yet: "it's david...
  is back?!?!?" and "you didn't cut the if you have any questions just
  whatsapped me", both flagged as regressions of things already believed fixed.
  Root-caused both from real transcript/render evidence before writing any
  code, per the user's standing instruction not to guess.)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**:
  - `pipeline_runner.py` (C43): `_fill_intro_lead_dead_space`'s self-intro-repeat
    guard (the original fix behind "it's David from...") compared caption
    **object identity** (`overlapping[0] is captions[0]`) to decide whether a
    detected gap was the speaker's own opening line repeating on-screen. Real
    transcript evidence (job_452ef6c48100): the self-intro is ONE spoken
    segment (0.21–4.61s) but gets split into MULTIPLE phrase-level captions —
    so the gap's first overlapping caption is never literally the *same object*
    as `captions[0]`, even though it's still time-contained within the first
    spoken segment. Fixed by comparing against `segments[0]`'s end time
    (+200ms slack) instead of caption identity — a fix that generalizes to any
    number of caption chunks the first sentence gets split into.
  - `content_planner.py` (C44): `plan_filler_removal`'s deterministic
    dup-phrase backstop (`_cut_duplicate_phrases` — the "no matter what the
    LLM concluded, re-scan with pure rules" guarantee described in its own
    docstring) was only ever reached via the branch taken after **every**
    verify retry is exhausted. The loop returns immediately the moment
    `verify_filler_removal` reports `{"clean": true}` **or** returns `None`
    (the documented "assume passed" fallback for when the verify LLM call
    itself fails/errors) — so if the LLM's own retake-review was wrong on
    attempt 0, or unavailable (plausible: the exact same job's ASR calls were
    hitting real ElevenLabs quota errors in this session), the deterministic
    backstop was dead code for that run. Real instance: job_452ef6c48100's
    "If you have any questions, just WhatsApp me directly" retake survived
    because verify said clean. Fixed by running the backstop on every loop
    iteration, before the verify call, not just after retries are exhausted.
- **Why / impact**: both are the same class of bug already named in Rule 13/15
  (a deterministic guarantee that doesn't actually run on every path is not a
  guarantee) — C43 is an identity-vs-time-containment mismatch scoped too
  narrowly for multi-caption sentences; C44 is a guard clause returning before
  the "always run this" step it was supposed to precede. Neither is a
  regression of the original fix's logic — both are the original fix's own
  narrower assumption breaking on a real input it didn't anticipate.
- **Status**: ✅ C43 covered by a regression test reproducing the exact
  multi-phrase-caption scenario; C44 covered by two new tests
  (`test_dup_phrase_backstop_runs_even_when_verify_says_clean_on_first_pass`,
  `..._returns_none`) plus a new real-data regression test
  (`test_cut_duplicate_phrases_preserves_dollar_figure_between_two_retakes`,
  from job_452ef6c48100's actual transcript) locking in that the fix doesn't
  over-cut the `$8,400` figure that sits directly between two genuine retakes
  in this video. Full suite passes. C44 re-verified against this job's real
  cached transcript end-to-end (fresh `_op_remove_filler` re-run): output
  duration matches the clean/deduped keep_ranges exactly, `$8,400` preserved,
  no duplicate. Separately investigated the user's "you keep cutting the
  8400 dollars" claim directly: scanned all 40 historical "David"-video job
  runs' final delivered props — `$8,400` present and correct in 100% of them;
  the one observed `$6,300`/`$8,400` mismatch was a transient mid-replan state
  that the existing vision-QA retry loop already caught and corrected before
  delivery, not a delivered defect. No separate fix made for that claim beyond
  C44 and the new regression test, since no reproducible delivered-content bug
  was found.

---

## 2026-07-21 (2) — Content-planning quality/consistency fixes from real user feedback (C40-C42)
- **Who**: Claude (same session — user reviewed the actual delivered videos from
  the day's harness runs and gave specific, concrete feedback: missing data
  cards/calendar for clearly-spoken numbers, popup cards dominating over a
  timeline they'd previously liked)
- **Branch/commit**: whatsapp-studio (`62ba5fd`, `0747454`, `7f5ced5`)
- **What changed**:
  - `content_planner.py` (C40): `_plan_gauge` now skips the card entirely if
    `title` is empty, matching every other visual type (`_plan_topic_card`/
    `_plan_corner_card`/`_plan_quote` already do this). Caught directly in a
    delivered video — a gauge with a blank title bar.
  - `content_planner.py` (C41): new 4th criterion in `_plan_quality_failures` —
    if the transcript has an explicit "$" amount and the plan has zero
    count_up/gauge/countdown/before_after cards at all, fail with the specific
    amount named. Root cause: SYSTEM_PROMPT's classification guidance was
    already correct, but LLM compliance was inconsistent run-to-run on the
    exact same transcript (same video, some replans produced a proper data
    card, others produced only generic topic_cards). Deliberately coarse
    (presence + total absence, not exact value matching) to stay low-risk.
  - `content_planner.py` (C42): `_plan_process_timeline` required an exact
    string match between `chapters[].label` and `process_timeline.
    chapter_label` — two independently-generated fields in the same LLM
    response with no structural guarantee they agree. Any mismatch silently
    discarded the *entire* timeline, and the orphaned takeover chapter then
    got fully evicted by the hidden-budget cap (which already knows to
    cap-not-remove a *real* timeline section) instead of showing it. Now
    falls back to the sole `takeover:true` chapter when exact match fails and
    there's exactly one candidate; stays honest (returns None) when 0 or 2+
    candidates make the fallback ambiguous.
- **Why / impact**: none of C31-C39 (the facecam-timing/render-reliability
  fixes from earlier the same day) touch content-planning *judgment* at all —
  these three are a separate axis (which visual type gets chosen, and whether
  a chapter's real intended graphic survives to delivery) that was still
  producing exactly the symptoms the user originally complained about
  ("brand style failed"-adjacent quality issues), just not literal degradation.
- **Status**: ✅ all 3 covered by regression tests (including explicit
  "don't guess when ambiguous" cases for C42), full suite passes. Not yet
  re-verified via a fresh harness run on the same real jobs (content-planning
  is non-deterministic, so confirming requires multiple runs) — next step
  before calling this fully closed.

---

## 2026-07-21 — Reconciled with origin + replica harness + 6 confirmed apply_style bugs (C34-C39)
- **Who**: Claude (session continuation — user asked to fetch latest origin, resolve
  conflicts, then root-cause "brand style rendering failed" for real using a
  repeatable local harness instead of live-WhatsApp trial and error)
- **Branch/commit**: whatsapp-studio (local commits `2a6b0e2`..`aa97270`; merged
  `origin/whatsapp-studio` in cleanly, no real conflicts — local's 24-commit lead
  turned out patch-identical to what PR #38 already merged upstream)
- **What changed**:
  - `main.py` (C34): uvicorn hardcodes `ProactorEventLoop` on Windows whenever
    `use_subprocess` is false — its accept() doesn't re-arm after a transient
    OSError (WinError 64), silently killing the server's ability to accept new
    connections with no crash. Bypasses `Server.run()`/`uvicorn.run()` entirely,
    drives `server.serve()` on a manually created `SelectorEventLoop`.
  - `webhook.py` (C35): `serve_file` was `async def` calling a blocking SQLAlchemy
    query directly — froze every concurrent Remotion video-fetch behind the first
    one on the single event-loop thread. Made it a plain `def` (FastAPI threadpools
    it automatically).
  - `content_planner.py` (C31) / `pipeline_runner.py` (C33, C38): `mode_schedule`
    → `scenes` gets frozen at various points, but content-zone visuals keep
    getting shifted/inserted afterward (`_resolve_same_slot_overlaps`,
    `_shift_off_dominant_windows`, C13's topicCard insertion, C37's section cap).
    Extracted a single `_recompute_scenes_from_content()` now called after every
    point that can still touch content-zone elements, not just once.
  - `pipeline_runner.py` (C39) — **the actual root cause behind C31/C33/C38's
    symptom**: `_insert_transition_holds` (existing Fix C21) treated card-growing
    and card-shrinking transitions identically, always arriving early at the
    *next* keyframe's size. Correct for shrinking (get the oversized card out of
    the way ASAP); backwards for growing (workflow → dominant) — it made the card
    jump back to full size ~20 frames after entering docked mode, regardless of
    how much longer real content needed that space. Now direction-aware: growing
    holds at the smaller size until just before the next real transition instead
    of arriving early.
  - `remotion-composer/.../StepList.tsx` (C32): inactive step rows stacked two
    independent dimming mechanisms (a muted color + a 0.32 opacity cut), collapsing
    contrast to near-invisible against the warm palette.
  - `remotion-composer/.../Captions.tsx` (C36): the karaoke-highlight sweep is a
    raw linear character count with no word-boundary awareness — splits a word
    into two colors on nearly every phrase. Snaps to the last fully-covered word.
  - `pipeline_runner.py` (C37): new deterministic guarantee for
    `facecam_never_restored` (speaker hidden through the video's own end) —
    caps the offending section's `toFrame` with runway to reappear, same
    cap-don't-remove pattern as Rule 4/D2.
  - New `whatsapp_mvp/simulate_job.py`: a committed replica harness (`retry`
    mode hits the real `/jobs/{id}/retry` endpoint; `apply-style` mode calls
    `_op_apply_style` directly for fast iteration) — replaces one-off scratch
    scripts, reports degraded status / facecam-overlap / props_lint findings
    directly. Used to find C36-C39 without a single live WhatsApp round-trip.
  - Discovered (not previously known to this session): `server/index.js` +
    `server/worker.js` (Node/Express + BullMQ) is the real, intended public
    front door — fast-ACKs webhooks, drops stale redeliveries, then calls the
    Python API (which should never be exposed directly). Local dev had been
    tunneling straight to the Python app all along, bypassing this. Now running
    Redis + both Node processes, tunnel repointed at the gateway's port 3000.
- **Why / impact**: closes the actual recurring "brand style rendering failed"
  WhatsApp message for the content shapes tested (2 different real videos, 24s
  and 50s, both confirmed clean end-to-end via the harness — see Rule 1). C39 in
  particular was the deepest root cause: a single wrong assumption in a helper
  function from 2026-07-17 that has likely been silently causing every
  "content-zone element overlapping an oversized facecam" report since.
- **Status**: ✅ all 6 fixes covered by regression tests, full suite passes,
  verified via 2+ independent live `/retry` runs on 2 different real jobs
  through the harness. One known minor residual: a brief (~20 frame) partial
  overlap can still occur right at the tail end of a content-zone element's
  own display window, during the growing-transition itself, if the element's
  endFrame runs right up against the transition point — much smaller in scope
  than the original bug (hundreds of frames), not yet fixed, not blocking
  delivery.

---

## 2026-07-19 — Fix C22: QA stills sampled frame 0 (before the SpeakerCard's own entrance renders anything) on every job, non-deterministic vision scoring turned it into recurring apply_style degradation
- **Who**: Claude (new session — user reported the WhatsApp degradation
  reminder message recurring and asked for a real fix + confirmation, not
  another "should be fixed")
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing)
- **What changed**:
  - `qa_stills.py`: `pick_qa_frames` no longer samples frame 0 under any rule.
    First attempt only special-cased the `_transition_windows` loop's
    `win_start - 8` sample (always `(0, X)` since `scenes[0]` is always
    `{"frame": 0, ...}`) — a live re-run (real render, real vision-QA call)
    against the corrected code degraded *again*, from a completely different
    rule in the same function (the "full-screen interval midpoint" sampler
    resolving to `[0]` when the intro-collapse transition is short enough that
    frame 0 is the only non-docked sample at a 30-frame stride — confirmed via
    that same live re-run, which regenerated a 20-frame collapse instead of the
    original 180-frame one). Final fix enforces the invariant once at the
    function's single return statement (`0 < f < duration_frames`) instead of
    patching each contributing rule individually.
  - `test_qa_stills.py` (new): covers both paths — the original
    transition-window case (`job_ac00838adea9`'s real `scenes` array) and the
    short-intro-collapse case the live re-run actually hit.
  - `whatsapp_mvp/CLAUDE.md`: added Rule 14 (full root-cause writeup for both
    paths, including the first-attempt miss and how it was caught; why this is
    a *different* bug from Rule 9/13 despite an identical-looking "空画布"
    vision label); added a cross-reference note at the end of Rule 9.
- **Why / impact**: this is the bug behind the actual WhatsApp message the
  user kept receiving ("提醒：品牌样式渲染这一步没成功…可回复 'retry' 重试")
  recurring across multiple real jobs, including *after* Rule 13/Fix C16
  landed the same day — a structurally different mechanism (a QA-sampling
  artifact content_planner cannot influence, not a content-planning gap it can
  retry its way out of) that Fix C16 was never going to close. Worth restating
  Rule 13's own general principle here too: the first fix attempt at *this*
  bug also turned out to be scoped to only one of several paths that produce
  the same artifact, and was only caught because a live end-to-end re-run was
  actually attempted instead of stopping at a green unit test.
- **Status**: 🧪 to verify — confirmed at the function level (`test_qa_stills.py`,
  both paths). A live `_op_apply_style()` re-run against `job_ac00838adea9`
  (no Redis/RQ worker needed — called directly via `job_manager.get_job` +
  `pipeline_runner._op_apply_style`, bypassing the queue entirely) is what
  caught the second path in the first place; that re-run itself degraded
  (correctly, since it ran against the pre-fix code at that point). **Whether
  the *corrected* code gets through a live re-run of this job without
  degrading has not been confirmed as of this entry** — that's the next thing
  to check before treating this as closed, and the C13-C22 work is still
  sitting uncommitted on `whatsapp-studio`.

---

## 2026-07-17 (2) — Deterministic guarantees for intro dead-space + content-free takeovers (C13-C16); Windows WinError 5 bundle-lock fix (C17)
- **Who**: Claude (background session, real-backtest-driven, user-flagged from a
  rendered screenshot)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing — see session)
- **What changed**:
  - `pipeline_runner.py`: Fix C14 — the `intro` branch's mode_schedule clamp now
    caps the *first* workflow (card-shrink) transition at
    `introOutFrame + 100` frames, not just the existing "don't start too early"
    clamp. Without this, the card stayed full-size until whenever the first real
    content item happened to be ready (observed: 226 frames / 7.5s after intro
    ended), regardless of how long that took.
  - `pipeline_runner.py`: Fix C13, `_fill_intro_lead_dead_space` — deterministic
    fallback for `intro_lead_dead_space` (Rule 9) surviving all 3 LLM retry
    rounds: inserts one lightweight caption-derived `topicCard` into the gap,
    mount frame computed from the real `scenes` transition windows (not a
    guessed offset), gated by a lint-before/after safety valve that rejects the
    insertion if it introduces any new finding type.
  - `pipeline_runner.py`: Fix C15, `_demote_content_free_takeovers` —
    deterministic fallback for `section_takeover_lacks_content` surviving all 3
    rounds: removes the section takeover + its speaker-hide opacity keyframes
    (both together — removing only one leaves a worse "hidden speaker, no blob
    either" state) when the section's real content is already covered by an
    independently-anchored content-zone element. Same safety valve as C13.
  - `pipeline_runner.py`: Fix C16 — C13/C15 were only being applied after the
    main props_lint retry loop; `_op_apply_style`'s vision-QA-triggered replan
    path (`props = _build(feedback=...)` when `qa_stills` finds a "high"
    severity issue) produced a second, completely separate `props` that never
    passed through either guarantee. This is why the first "verified" render
    still shipped the bug. Extracted both into `_apply_deterministic_guarantees`
    and call it after every path that can yield final props, re-writing
    `props_path` after the second call site too.
  - `remotion_bundle.py`: Fix C17 — Windows-only `[WinError 5] 拒绝访问` on
    bundle rebuild (source: a teammate's `WINDOWS_REMOTION_WINERROR5.md`).
    `ensure_remotion_bundle` now cleans up stale `.build.tmp-*` leftovers
    (never touches a valid `build` cache) before every rebuild attempt, and
    retries once specifically on a `WinError 5` stderr match.
  - See `whatsapp_mvp/CLAUDE.md` Rule 13 (C13-C16) and Rule 12 (C17) for the
    full writeups.
- **Why / impact**: user directly flagged (from a screenshot of an actual
  delivered render) that the intro dead-space fix "hadn't done anything" —
  investigation confirmed the *detection* (Rule 9) had been firing correctly
  all along, but the LLM's own replan never once resolved it in 3 rounds
  across 3 separate backtests. A second, visually identical-looking bug
  (a full-canvas section takeover rendering as just a title + decorative blob,
  same signature as the already-fixed Rule 4 but a different root cause) was
  found in the same investigation. The first fix attempt (C13/C15 alone)
  looked verified — full render succeeded, props-level checks passed — but a
  second full backtest run showed the bug still present, which led to finding
  the C16 bypass: the deterministic guarantees were real but only wired into
  one of two code paths that can produce final props.
- **Status**: ✅ verified — C14/C13/C15 confirmed via direct function tests
  against real captured findings, then via a fresh end-to-end `_op_apply_style()`
  run with a frame pulled directly from the actual rendered mp4 (not a props
  check, not a QA still) at the former bug location, confirming the speaker
  visible with real content instead of the empty gap / decorative blob. C16
  confirmed by re-running verification after the fix and seeing
  `section_takeover_lacks_content`/`facecam_hidden_too_long` actually absent
  from the final `props_lint.json`, where the first (unfixed-C16) run still
  had them despite C13/C15 existing in code. C17 verified via a direct test of
  the cleanup function only (fake stale dir removed, real `build` cache
  untouched) — not yet exercised against a real WinError 5 (transient/rare to
  reproduce on demand).

---

## 2026-07-17 — ElevenLabs 401 root-caused as quota_exceeded (not a bad key); faster-whisper fallback installed; GOP/keyframe fix for Remotion render crashes
- **Who**: Claude (background session, real-backtest-driven)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing — see session)
- **What changed**:
  - `tools/video/video_trimmer.py` (concat re-encode), `tools/enhancement/face_enhance.py`,
    `tools/enhancement/color_grade.py`: all three now pass `-g <fps>` (30) alongside
    the existing `-fps_mode cfr -r <fps>`. Fix C12 — Remotion's render was crashing
    with `"No frame found at position N"` because keyframe interval was never set
    anywhere in the chain, so libx264 defaulted to a 250-frame GOP (confirmed via
    ffprobe: 4 keyframes in 1119 frames, none before frame 250). The existing retry
    logic in `_op_apply_style` blamed this on a transient qa_stills/render cache
    race — a manual, standalone re-run (no qa_stills nearby) hit the same error at
    an even earlier frame, ruling that out. Verified end-to-end: same real props
    file that failed twice now renders cleanly (1122/1122 frames) after the fix,
    with keyframes going from 4→37 (evenly spaced ~1s apart). See
    `whatsapp_mvp/CLAUDE.md` Rule 11.
  - `faster-whisper` (1.2.1, matching the pinned version) installed into the Python
    environment the live server actually runs on. It was listed as a dependency but
    never installed, so the code's designed fallback-on-ElevenLabs-failure path was
    silently dead — a transcription failure meant `remove_filler` no-op'd entirely
    (`_op_remove_filler` returns "unchanged" when `_safe_transcribe` returns None)
    instead of degrading to lower-quality local transcription. Verified working via
    a direct smoke test and a full pipeline backtest that genuinely exercised the
    fallback path.
  - `pipeline_runner.py`: `_transcribe_elevenlabs` (Fix C11) now catches
    `requests.HTTPError` specifically and parses the response body's
    `detail.code`/`detail.message` instead of only logging
    `resp.raise_for_status()`'s generic "401 Client Error: Unauthorized". A
    quota-exhausted request now logs `"quota_exceeded: ...免费档配额用完——等
    月度重置或升级套餐，换 key 无效"` instead of an indistinguishable-from-a-
    bad-key 401. See `whatsapp_mvp/CLAUDE.md` Rule 10.
- **Why / impact**: two local backtest runs (raw `raw_demo1/dajaai2.mp4` through
  `remove_filler` → `apply_style`, run to genuinely verify the 2026-07-16
  `intro_lead_dead_space` fix end-to-end) both failed transcription with the same
  401 already "fixed" the day before. Direct `curl /v1/user` with the same key
  returned 200, ruling out a bad key; only reading the `/v1/speech-to-text`
  failure's JSON body (not just its status code) surfaced
  `code: "quota_exceeded"` — the ElevenLabs account is on the **free tier
  (10,000 characters/month)**, and normal usage plus this session's own testing
  had exhausted it (2 credits remaining at time of writing, resets 2026-07-23).
  **This is not fixable in code** — a new API key only resets the clock (a fresh
  signup gets the same tiny free-tier cap), so real WhatsApp users will keep
  hitting this until the plan is upgraded or the monthly reset passes. Flagged to
  the user directly; no plan change made without their decision.
  Installing `faster-whisper` unblocked a third backtest run that got much
  further: `remove_filler` ran for real for the first time (39.04s → 37.3s), which
  also gave concrete (not hypothetical) evidence for why ElevenLabs was made
  primary over local whisper — a mis-tokenized code-switched name ("Dixon" → "D"
  / "ixon") triggered a false-positive stutter flag (self-corrected by the
  existing recheck pass) and a genuine leftover duplicate phrase slipped through
  uncut. `apply_style` then got further than any prior run (real content, no more
  "empty canvas" vision-QA verdicts, `intro_lead_dead_space` fired correctly a
  third time) but hit a *new* failure — the Remotion render itself crashing,
  root-caused and fixed as Fix C12 (GOP/keyframe interval, see above and
  `whatsapp_mvp/CLAUDE.md` Rule 11). Verified by regenerating the same job's
  enhancement-chain output with the fix and re-rendering against the same real
  props that had failed twice — succeeded cleanly.
- **Status**: ✅ verified — quota-exhaustion error message (C11) confirmed correct
  in a live run; faster-whisper fallback confirmed exercising the real fallback
  path end-to-end; GOP fix (C12) confirmed turning a twice-reproduced render
  crash into a clean 1122/1122-frame render on the same real job. Still 🧪 open:
  the ElevenLabs plan/quota itself (needs a user decision — upgrade vs. wait for
  the 2026-07-23 reset) and a full-quota re-run to see `intro_lead_dead_space`
  resolved by ElevenLabs-quality transcript data rather than whisper's.

---

## 2026-07-16 — Animation-quality pass: process_timeline unlocked for warm mode, criterion-loop coverage extended, backtest-driven fixes
- **Who**: Claude (background session, real-backtest-driven)
- **Branch/commit**: whatsapp-studio (uncommitted at time of writing — see session)
- **What changed**:
  - `config.py`: Fix — `load_dotenv(..., override=True)`. Without it, a stale
    `ELEVENLABS_API_KEY` (or any var) already set in whatever shell launched the
    server would silently outlive `.env` edits and server restarts forever — a fixed
    key in the file had zero effect until this landed. See `whatsapp_mvp/CLAUDE.md`
    Rule 8.
  - `props_lint.py`: new check `intro_lead_dead_space` (Fix C10) — catches a
    content-zone gap between `introOutFrame` and the first content-zone element's
    mount, a real gap in coverage between `takeover_dead_space` (hidden-speaker-only)
    and `content_planner`'s `_sparse_gaps` (8s bar, deliberately loose). Confirmed
    on a real WhatsApp job (`job_dc6a22198c6d`) whose `apply_style` step fully
    degraded — user got only the bare trimmed video — because vision QA caught a
    3.7s empty content zone right after intro that neither deterministic check was
    scoped to see. See `whatsapp_mvp/CLAUDE.md` Rule 9.
  - `content_planner.py`: D3 (speaker restored before video end), D4 (takeover section
    start anchored to first real content, not the raw chapter boundary), D5 (timeline
    hard-cap sized to actual available budget instead of a fixed 8s guess that's
    mathematically impossible to fit on any video <27s), D6 (stopped force-overriding
    `dark: true` on every process_timeline chapter).
  - `pipeline_runner.py`: C5 (props_lint retry loop bounded + best-of, was single-retry),
    C6 (best-of comparison is now richness-aware — was findings-count-only, which let a
    content-free replan "win" over a richer-but-imperfect one), C9 (fixed a disk/memory
    desync where the props_lint loop's winning candidate wasn't always what actually got
    written to the file the render command reads from).
  - `props_lint.py`: new checks `element_mounts_during_card_transition`,
    `low_visual_richness`, `section_takeover_lacks_content`.
  - `qa_stills.py`: transition-boundary frame sampling, 4 new vision-checklist items
    (Transitions/Reference-match/Cohesion/Beat-to-caption-sync), wired the pre-existing-
    but-unused `batch-stills.mjs` in with per-job `inputProps` support (measured 2.9x
    speedup on a real job: 50.1s → 17.1s for the same 6 stills).
  - `remotion-composer/src/components/xiaojin/TimelineSection.tsx` +
    `SectionLayer.tsx`: TimelineSection now reads `theme.ts` palettes instead of
    hardcoded dark-canvas colors; SectionLayer no longer gates it behind
    `mode === "dark"`.
  - `remotion-composer/src/components/xiaojin/{StatsHookIntro,TitleImpactIntro,
    ChipsIntro}.tsx` (new): the 3 previously-unported video-studio intro patterns,
    wired through `XiaojinEditorial.tsx` + `contracts/render_props.schema.json` +
    `content_planner.py`'s SYSTEM_PROMPT, defaulting to the original `title_card`
    behavior when unset.
- **Why / impact**: Every fix here traces to a concrete finding from real, genuine
  end-to-end backtests (dajaai-walking-fresh, MrBeastRaw) run through the actual
  FastAPI server + Agent SDK pipeline, not synthetic repros alone — see
  `whatsapp_mvp/CLAUDE.md` Rules 4 and 5 for the two most subtle ones (empty
  full-canvas takeovers, and the criterion-loop disk-desync bug). ⚠️ contract
  change — `render_props.schema.json`'s `intro` object gained optional
  `variant`/`brandLabel` fields (additive, backward-compatible, existing fixtures
  still validate).
- **Status**: 🧪 to verify — unit/script test suite green (21 pytest + 97+ content_planner
  checks + golden extraction + props_lint), TSX `tsc --noEmit` clean, D6 verified via
  direct still render, C9 verified by code review + real-job reproduction. A fresh
  end-to-end backtest re-confirming D6+C9 together in the live pipeline was still
  running at last check — see session for outcome.

## 2026-07-08 — P3 hotfix: stop demo defaultProps leaking into real renders; ioredis error listener
- **Who**: P3
- **Branch/commit**: feat/template-and-gateway / d53bfdd
- **What changed**: Removed the demo `intro`/`compliance` keys from `XiaojinEditorial`'s `defaultProps` in `Root.tsx` (Remotion's `--props` merges per-key rather than replacing wholesale, so a real job that didn't set these was falling back to "示例视频"/"Demo Agent"). Added a `client.on("error", ...)` listener in `createRedis()` (`server/index.js`) so a Redis connection drop logs and lets the existing `retryStrategy` reconnect instead of crashing the gateway process.
- **Why / impact**: Fix ① would otherwise put demo data on every real render P1 tries during the Step 3 end-to-end acceptance. Fix ② addresses the gateway silently dying that P2 hit twice during testing. No contract change.
- **Status**: ✅ Both fixes verified — tsc clean; rendered stills confirm compliance/intro chrome only appears when supplied, never as demo fallback; `server/index.js` syntax-checked. Landed as commits on the still-open PR #1 (`feat/template-and-gateway` → `whatsapp-studio`).

## 2026-07-08 — P3 closes out template-and-gateway: build proven, fixture coverage, rebased
- **Who**: P3
- **Branch/commit**: feat/template-and-gateway / 887a31e
- **What changed**: XiaojinEditorial template + 15 components, contract-② (gauges/countdowns/calendarEvents/qrContact), gateway `/files/:jobId/:filename` proxy, styles. This round: fixed `tsc --noEmit` (21 errors — every composition's Props interface was missing the index signature Remotion's `Composition<Props extends Record<string, unknown>>` requires; added `extends Record<string, unknown>` to all 9, plus two unrelated pre-existing bugs in ProviderChip/Explainer); confirmed `npx remotion render XiaojinEditorial` produces a valid mp4; added `contracts/fixtures/render_props.full.example.json` exercising gauges/countdowns/calendarEvents/qrContact (previously untested — rendered + spot-checked stills, all four appear correctly); verified contract-② is already in sync with P2's `_op_apply_style`/`content_planner.py` (field names and required sub-keys match exactly); confirmed no duplicate `XiaojinEditorial.tsx` or `/files` proxy exists on P2's side; rebased onto latest `whatsapp-studio` (kept `whatsapp-mvp-progress.md` deleted).
- **Why / impact**: Unblocks `apply_style` end-to-end — P2's planner output can now actually render through to a playable video via a build-verified template. No contract change (schema was already correct; the gap was verification, not shape).
- **Status**: ✅ Tasks 1–5 of `P3-instructions.md` complete (tsc clean, render proven, fixture coverage, contract sync confirmed, dedup confirmed, rebased). Ready for P1/P2 to re-verify end-to-end with a real WhatsApp job.

## 2026-07-07 — Canonical docs established: STATUS + this CHANGELOG
- **Who**: P1
- **Branch/commit**: whatsapp-studio
- **What changed**: Added `docs/STATUS.md` (authoritative state snapshot) and this changelog. Retired the stale `whatsapp-mvp-progress.md` (codex leftover) and scattered snapshots.
- **Why / impact**: Progress info was scattered and often stale. From now on **only maintain these two**.
- **Status**: ✅

## 2026-07-07 — P1 completes the L2 wiring (baton closed)
- **Who**: P1
- **Branch/commit**: whatsapp-studio
- **What changed**: `agent_editor.py` wires `remove_filler`/`apply_style` into the L2 tool vocabulary (ALL_TOOL_SCHEMAS/OP_TOOLS/_simulate/SYSTEM_BASE) + zero-instruction default + double-subtitle guard; manifest now via `lib.pipeline_loader`; `worker.py` runs `source_media_review`.
- **Why / impact**: Closes the "L2 side still missing" baton P2 handed over in round 4. **P2 must re-merge whatsapp-studio.** End-to-end restored for text-instructed editing requests.
- **Status**: 🧪 pending P2 re-merge + e2e verification

## 2026-07-07 — P2 round 4: lane correction (revert L1.5 + merge upstream + /files proxy)
- **Who**: P2
- **Branch/commit**: feat/pipeline-capabilities / 960b470, 53ed45b, 322d8b3, 777758f
- **What changed**: Reverted the planning logic previously wired into L1.5 (`llm_planner` back to frozen); merged whatsapp-studio (worker routes to L2 primary); ported the `/files` proxy into the gateway; 4 test suites pass; documented the baton.
- **Why / impact**: Follows the lane decision in `docs/L1.5-vs-L2.md` (L2 only). **Gateway /files is ready** — one less thing for P3.
- **Status**: ✅ (fully closed once P1's L2 wiring lands)

## 2026-07-07 — P1 executes the integration merge (base = P2 latest + our 4 fixes)
- **Who**: P1
- **Branch/commit**: whatsapp-studio (merged `pipeline_runner.py`)
- **What changed**: Merged P2's handler/render layer (apply_style/remove_filler/content_planner/build_xiaojin_scenes/qa_stills…) with our fixes (trim_leading_silence fix, T1, main-loop remove_segment descending order, transcribe reuses _safe_transcribe). Kept the L2 lane; did NOT take P2's L1.5 wiring.
- **Why / impact**: Both lines changed pipeline_runner; merged function-by-function to avoid losing fixes.
- **Status**: ✅ tests pass, committed

## 2026-07-07 — P1 froze contracts + deepened L2 + lane doc
- **Who**: P1
- **Branch/commit**: whatsapp-studio
- **What changed**: Froze the 3 contracts (op_registry/render_props/style_params) + fixtures; T1 transcript-awareness; `lib.pipeline_loader` replaces home-rolled manifest reading; `docs/L1.5-vs-L2.md`; `whatsapp_mvp/CLAUDE.md` working conventions.
- **Why / impact**: ⚠️ Contracts frozen — future changes need 3-way sync; planning unified on L2.
- **Status**: ✅

## 2026-07-07 — P2 round 3: content-driven layout
- **Who**: P2
- **Branch/commit**: feat/pipeline-capabilities / 0dc41ee, eb7dce3
- **What changed**: `build_xiaojin_scenes` (beat-driven scenes), `build_caption_phrases` (phrase captions), `qa_stills`, safe zones; DataCards + contract-② fixes.
- **Status**: ✅ (folded into merged version)

## 2026-07-07 — P2 round 2: face calibration + robustness
- **Who**: P2
- **Branch/commit**: feat/pipeline-capabilities / 93aa41e
- **What changed**: `calibrate_speaker_object_position` (face_tracker framing); `reference_analyzer` wiring; `_safe_transcribe` (fixes transcription crash).
- **Why / impact**: ⏸ face calibration **not verified on real videos** (opencv/mediapipe won't install locally).
- **Status**: 🧪 calibration to verify

## 2026-07-07 — P2 round 1: handler layer landed
- **Who**: P2
- **Branch/commit**: feat/pipeline-capabilities / 7623b6f
- **What changed**: `content_planner` (chapters/data cards/word-level filler), `_op_apply_style` (renders XiaojinEditorial), `_op_remove_filler`, `reference_analyzer`.
- **Status**: ✅ (folded into merged version)

---

## Backlog (move up + fill in when done)
- [ ] P1: `apply_style` graceful degradation (don't crash on render failure)
- [x] P3: create `feat/template-and-gateway`, build-verify XiaojinEditorial, consume contract-② props (2026-07-08)
- [ ] P1: wire `resolve_reframe_op` into L2; consume reference style_params
- [ ] P1+P3: multi-media job model + reference upload / role assignment
- [ ] P1/P2: wire compose-stage OM tools (color_grade / audio_enhance first)
