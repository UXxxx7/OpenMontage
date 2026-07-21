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

## Rule 6 — the stacking system's "different chapter = new stack at the top" reset isn't collision-safe by itself

Confirmed real bug (2026-07-16, found by the user frame-by-frame reviewing a genuine
WhatsApp-delivered video, `job_73e873e4f7e1`): a `dataCard` ("Total Duration: 5
months") and a `topicCard` ended up at the exact same `(x, y)` with overlapping
mount/end windows. Since `topicCards` render after `dataCards` in
`XiaojinEditorial.tsx`'s layer order, the topicCard painted directly on top — the
dataCard's content was **never visible on screen at all**, not just visually
cluttered. `props_lint`'s `element_overlap` check caught it, but the props_lint
retry loop couldn't reliably fix it (a fresh LLM replan has no guarantee of
avoiding the same coincidence again).

Root cause: the content-zone stacking loop (`_flush_stack` and the `fits`/`else`
branch around it, `_to_frame_plan`) resets a new stack's `entry_y` to
`_CONTENT_ZONE_Y` whenever a data point belongs to a different chapter than the
current stack — but doesn't verify the *previous* stack has actually finished
being visible on screen before doing so.

**The fix (Fix E2):** `_resolve_same_slot_overlaps` — a deterministic post-pass
that runs once, after all stacking is done, over every content-zone element. Any
pair sharing the exact same `(x, y)` with an overlapping time window gets the
later-mounting one delayed to start right after the earlier one's `endFrame` (+
buffer), preserving its own display duration (shift, not compress). This is
**not** an LLM retry — it's a guarantee, the same class of fix as D3/D4/D5/D6.
User's own framing for why this approach over retrying: *"if you don't have time
to put [content in], then don't put and just extend the previous section"* —
i.e. delay the newcomer rather than risk silently hiding the incumbent.

**If you touch the stacking loop again:** a "new stack, new position" reset is
only safe if you also prove the old stack is gone by then. Don't assume it —
`_resolve_same_slot_overlaps` exists as the backstop precisely because that
proof doesn't hold in every case.

## Rule 7 — "long enough to fit the content" beats "short and cram everything in"

Confirmed real bug (same `job_73e873e4f7e1` review): `TimelineSection`'s 3 nodes
revealed only 30 frames (1s) apart — but the component's own entrance animation
(`GROW_WINDOW`) takes 25 of those frames, leaving almost no settled, readable
time before the next stage started animating in. User's words: *"everything just
appears and disappears too fast."*

**The fix (Fix E1):** `TIMELINE_NODE_MIN_GAP_FRAMES` raised from 30 to 60 (2s).
More importantly, the section's own `toFrame` for a `has_timeline` chapter is no
longer just `natural_end` (the next chapter's boundary) — it's
`max(natural_end, last_node_revealFrame + _TAKEOVER_CONTENT_HOLD_FRAMES)`. If the
chapter's natural span is too short to give every stage proper pacing, the
section **extends past it** rather than cramming stages into whatever time
happens to be available. This only ever pushes later, never truncates — a
naturally-long chapter is unaffected. Composes correctly with the existing D2/D5
30%-hidden-budget cap: if there truly isn't enough budget, D5 still enforces the
ceiling, but within whatever budget exists, E1 spaces nodes out as much as
possible instead of packing them arbitrarily tight. Verified: same content that
used to compress 3 stages into a 90-frame (bare-minimum) section now spans
enough frames for each stage to fully animate and hold.

**General principle, stated by the user, worth keeping close by:** when there
isn't enough real content/time to do something properly, extend the
neighboring section rather than rushing the new one through. Don't treat "keep
the runtime exactly where the LLM's raw chapter boundary put it" as sacred if
honoring it means nothing gets time to be read.

## Rule 8 — `.env` changes need `load_dotenv(..., override=True)`, or a stale shell var wins forever

Confirmed real bug: fixed an invalid `ELEVENLABS_API_KEY` (401 Unauthorized — wrong
permission scope) by writing a corrected key into `.env`, verified the new key
worked with a direct API call, then had the user restart the server. The very
next real job still hit the same 401 from ElevenLabs. Process start time was
confirmed *after* the `.env` edit (`wmic`/`Get-CimInstance` on the actual PID
bound to port 8000), so it wasn't a restart-timing race — the new value was in
the file, and the process started after it was written, but the old value was
still what got used.

**Root cause:** `whatsapp_mvp/config.py` called `load_dotenv(_PROJECT_ROOT /
".env")` with no `override=True`. `python-dotenv`'s default behavior is to
**never overwrite a variable that's already set in the process environment** —
it only fills in ones that are missing. If the terminal window used to launch
the server ever had `ELEVENLABS_API_KEY` set directly in that shell session
(typed once during earlier testing, a leftover `$env:`/`export`, anything),
every subsequent restart *in that same window* silently re-inherits the stale
shell value and ignores whatever `.env` now says — no error, no warning,
indistinguishable from the fix "not having worked."

**The fix:** `load_dotenv(_PROJECT_ROOT / ".env", override=True)` — `.env` is
now always authoritative for local dev, full stop, regardless of what any
launching shell happened to have set.

**General principle:** whenever "I changed the config file and restarted, but
the old behavior persisted" comes up, don't stop at re-verifying the file
content — check whether the loader actually treats the file as authoritative.
A restarted process is not proof of a fresh environment if the loader silently
prefers whatever the shell already had. This also means: after this fix, a
config change genuinely requires nothing but a server restart — no need to
open a brand-new terminal window to dodge shell-level staleness.

## Rule 9 — a "card visible but content zone empty" gap needs its own check; `takeover_dead_space` only covers hidden-speaker spans

Confirmed real bug (`job_dc6a22198c6d`, live WhatsApp test): the whole `apply_style`
step degraded — user got back a bare trimmed video with zero brand styling — because
vision QA found "空画布" (empty canvas, high severity) at the still sampled right after
intro-out, the retry didn't fix it in time, and the job fell through to
`_DEGRADABLE_OPS`'s graceful-delivery path. Root cause, found by actually opening the
flagged still image rather than trusting the label: the frame was NOT literally empty
(SpeakerCard + ChapterNav + caption + rainbow bar were all present, exactly as
designed) — but there was a real ~260px blank cream rectangle in the content zone,
because `introOutFrame=80` and the first content-zone element (a countdown) didn't
mount until frame 190, a 110-frame (3.7s) stretch with nothing scheduled.

**Why the existing checks missed it, both of them:**
- `props_lint.py`'s `takeover_dead_space` only scans gaps **inside `hidden_spans`**
  (SpeakerCard fully hidden — a `sections` takeover). This gap happened while the card
  was visible and large (Intro mode), a completely different code path the check never
  looked at.
- `content_planner.py`'s `_sparse_gaps` (`RICHNESS_WINDOW_FRAMES = 8 * FPS`) is
  deliberately loose — it's meant to tolerate normal dialogue-only stretches, not flag
  every quiet moment. A 3.7s gap is well under its 8s bar by design. **Both checks were
  working exactly as built; neither was built to catch this specific pattern.**

**The fix (Fix C10):** new `props_lint.py` check `intro_lead_dead_space` — measures the
gap between `introOutFrame` and the earliest content-zone element's `mountFrame`; flags
it past the same `_DEAD_SPACE_FRAMES` (60 frames / 2s) bar already used for
`takeover_dead_space`, and feeds back an actionable instruction: if the gap covers a
pure greeting/self-intro line with no number/tool name to anchor to, add a lightweight
non-numeric element (topic_card/corner_card showing the brand or speaker's identity)
rather than leaving the zone empty until the first real keyword lands. This plugs into
the *existing* `_op_apply_style` props_lint retry loop with zero special-casing — the
loop already feeds every finding's `detail` text back into a content_planner replan
generically. Verified: fires exactly on the real job's captured props (gap 80→190,
110 frames), does not fire when a gap is under 60 frames or when there are no
content-zone elements at all (left to the richness/zero-visual checks, not duplicated
here).

**General principle:** two deterministic checks that each look "reasonable" and each
pass their own tests can still leave a real visual defect completely uncovered, if
they're scoped to different states of the same variable (hidden vs. visible) or
tuned for different purposes (density-over-time vs. this-specific-frame-looks-empty).
When a bug slips through the whole criterion-loop stack (cheap props_lint AND the
LLM's own quality gate AND the retry) and only the expensive vision-render step
catches it, that's a signal a *new* deterministic check is missing, not that the
existing ones need loosening or that vision QA was wrong to flag it. Always open the
actual flagged still before deciding what's broken — "空画布" was accurate in the
sense that mattered (unfilled content zone), even though the frame itself wasn't
literally blank.

**Not every "空画布" finding is this bug — see Rule 14 for a different one with the
same label.** This rule's case had real content on screen (SpeakerCard + ChapterNav +
caption + rainbow bar) that just didn't fill enough of the content zone. Rule 14's
case is a literal blank frame — nothing rendered at all — caused by QA's own frame
sampling, not by content_planner's output. Check `frame_index` against `pick_qa_frames`'
output before assuming it's the same root cause: frame 0 (or whatever the first
still happens to be) pointing at nothing is Rule 14; a mid-video still with visible
chrome but a sparse content zone is this rule.

## Rule 10 — ElevenLabs 401 can mean "key invalid" OR "quota_exceeded"; they look identical unless you read the response body

Confirmed real bug (2026-07-17, found while backtesting Rule 9 locally against
`raw_demo1/dajaai2.mp4`): two full pipeline runs in a row failed transcription with
`401 Client Error: Unauthorized for url: https://api.elevenlabs.io/v1/speech-to-text`
— the exact same symptom, and the exact same code path
(`_transcribe_elevenlabs`/`resp.raise_for_status()`), as the earlier "bad API key"
bug documented informally in this session's chat history. A direct `curl
https://api.elevenlabs.io/v1/user` with the same key returned `200` with real
account data, ruling out an invalid/expired key. Only reading the **response body**
of the failing `speech-to-text` call (not just the status code) revealed the actual
cause: `{"detail":{"code":"quota_exceeded","message":"This request exceeds your
quota of 10000. You have 2 credits remaining..."}}` — the account is on
ElevenLabs' **free tier (10,000 characters/month)**, and normal usage (real
WhatsApp jobs + this session's own backtesting) had nearly exhausted it.

**Why this is easy to misdiagnose as a key/config bug:** `requests`'
`resp.raise_for_status()` raises with only the generic reason phrase ("401
Client Error: Unauthorized for url: ...") — the JSON body carrying `code:
"quota_exceeded"` vs. some actual auth failure was being discarded entirely.
Swapping in a fresh API key "fixes" a quota-exhausted 401 too (a new ElevenLabs
signup gets its own fresh 10,000/month allowance), which is exactly why the
earlier fix *looked* correct at the time — but a fresh account has the same tiny
free-tier cap, so it silently recurs, and swapping keys again would too. **Only a
paid plan (or waiting for the monthly reset) actually fixes quota_exceeded; a new
key alone does not, it just resets the clock.**

**The fix:** `_transcribe_elevenlabs`'s `except` block now catches
`requests.HTTPError` specifically, parses `e.response.json()["detail"]`, and if
`code == "quota_exceeded"` logs and returns a message that says so explicitly
(`"quota_exceeded: ...免费档配额用完——等月度重置或升级套餐，换 key 无效"`)
instead of the bare "401 Unauthorized". Falls back to the generic message for any
other/malformed error body so a genuinely-bad key still logs clearly too.

**General principle:** when an HTTP client raises from `raise_for_status()`, that
exception string is often *only* the status line — many APIs (ElevenLabs included)
put the actually-useful diagnosis in the JSON body, which `raise_for_status()`
never looks at. Before spending time re-diagnosing "same error as before, is the
fix broken?", read the actual response body of the current failure — don't assume
a repeated status code means a repeated cause.

## Rule 11 — "No frame found at position N" can be a GOP/keyframe problem, not just a CFR problem; the existing retry papers over it without fixing it

Confirmed real bug (2026-07-17, found while backtesting Rule 9/10 end-to-end against
`raw_demo1/dajaai2.mp4` once ElevenLabs quota + missing faster-whisper fallback were
both worked around): `apply_style`'s Remotion render failed with `Compositor error:
No frame found at position N for source ...` — twice, on two different automatic
attempts, at two different positions (`266752` then, on a fully independent manual
re-run well after any `qa_stills` activity, `3072` at frame 6). The existing code
comment on the render retry (`_op_apply_style`, the `for attempt in range(2):` loop)
attributes this error to a **transient Remotion asset-cache race between
`qa_stills`' still-renders and the full render** — but a manual re-run, done
standalone with no `qa_stills` anywhere nearby in time, hit the identical symptom
at an even earlier frame. That ruled out the "transient timing" theory for this
occurrence.

**Actual root cause:** `ffprobe`'s frame-by-frame `pict_type` on the failing
source showed only **4 keyframes across 1119 frames** (gaps of ~250 frames /
~8.3s, and — critically — no keyframe before frame 250 at all, so frames 0–249
have nothing to decode from). 250 is libx264's own default `-g` (keyframe
interval) when nothing overrides it. `-fps_mode cfr -r 30` (already present
everywhere, fixing an earlier, different "No frame found" trigger — frame-rate
drift) says nothing about keyframe spacing. **`-g` was never set anywhere in the
video pipeline** (`tools/video/video_trimmer.py`'s concat re-encode,
`tools/enhancement/face_enhance.py`, `tools/enhancement/color_grade.py` — checked
all three, confirmed absent in each). Remotion's Rust compositor does
frame-accurate random-access seeking (6x concurrency during render, so it jumps
around, not a linear scan) and can't reliably decode a frame that far from its
nearest keyframe — this is a fundamentally different failure mode from the CFR
drift bug, even though Remotion reports the identical error string for both.

**The fix (Fix C12):** added `-g <fps>` (30, one keyframe/second — matches
`content_planner.py`'s hardcoded `FPS=30`) alongside the existing `-fps_mode cfr
-r <fps>` in all three re-encode call sites above. `tools/audio/audio_enhance.py`
needed no change — it already does `-c:v copy` (audio-only step, doesn't touch
GOP structure). Verified end-to-end: regenerated the same job's enhancement chain
output with the fix, confirmed via `ffprobe` the keyframe count went from 4→37
(evenly spaced ~30 frames apart, down from a 250-frame max gap), then re-ran the
exact same `npx remotion render` command against the same real props file that had
failed twice before — succeeded cleanly, 1122/1122 frames, first fully-successful
`apply_style` render in this entire investigation.

**On the existing retry-with-a-comment:** leave the 2-attempt retry in
`_op_apply_style` in place — it's cheap insurance and may still help for the
*original*, CFR-drift trigger of this same error string — but don't trust its
comment as a complete explanation next time this error shows up. A retry that
"usually works" is exactly the kind of signal that hides a second, different root
cause behind an already-explained symptom.

**General principle:** when a fix already exists for a bug with a given error
message, and that same error message recurs, don't assume it's the same bug
recurring (or a flaky repeat of the "already understood" cause) — check the
actual state that mechanism depends on (here: keyframe spacing, not frame rate)
before trusting the existing retry/comment to have the full picture. Two
different underlying defects can produce byte-for-byte the same error text from
a third-party tool.

## Rule 12 — Windows-only: a stale `.build.tmp-*` from an interrupted bundle rebuild causes `[WinError 5] 拒绝访问` on the next one

Source: a teammate's independently-written `WINDOWS_REMOTION_WINERROR5.md` (shared
2026-07-17), cross-checked against `remotion_bundle.py` and fixed in code (Fix C17)
rather than left as a manual-only runbook.

**What happens:** `npx remotion bundle`'s last step renames its temp output dir
(`.build.tmp-xxxx`) to `build`. If `build` already exists — most commonly because a
previous bundle rebuild was interrupted before it reached the rename (process
killed, machine slept, render timeout) — or an external process (antivirus scan,
an open Explorer/editor window) is holding a handle on it, Windows refuses the
rename and Remotion reports `[WinError 5] 拒绝访问`. **Windows only** — Linux/macOS
don't have this failure mode. `apply_style` catches it and gracefully degrades
(bare cut delivered, no template), same as any other render failure.

**Confirmed live in this session, not just theoretical:** partway through today's
backtesting I `taskkill`'d a stuck verification script (PID 5956) that was mid
`apply_style` — if it had been mid bundle-rebuild at that exact moment, it would
have left exactly this kind of stale `.build.tmp-*` behind for the next run to
collide with. `_BUNDLE_LOCK` in `remotion_bundle.py` only guards against concurrent
rebuilds *within the same running Python process* — it does nothing about a
leftover directory from a *previous* process that no longer exists.

**The fix (Fix C17):** `ensure_remotion_bundle` now calls
`_clean_stale_build_artifacts` (deletes any `.build.tmp-*` glob matches — never
touches `build` itself, since a valid, cache-fresh `build` is the normal reuse
path checked just above via the `marker.mtime` comparison) immediately before
attempting a rebuild, and retries the bundle subprocess once (after a 1s pause +
another cleanup pass) specifically when its stderr contains `WinError 5` — handles
a brief transient external lock (e.g., antivirus touching a just-written file)
without a manual restart. Verified the cleanup itself directly: created a fake
`.build.tmp-testfake123` dir, confirmed it's removed while a real, unrelated
`build` dir (from a previous session) is left untouched.

**What this does NOT fix, by design:** a *persistent* external lock (antivirus
actively holding the folder open, an Explorer window pinned on it) will still fail
after the one retry — the original doc is explicit that no code-side fix survives
that case, and a bad retry loop hammering a persistently-locked directory would
just waste time. That remains a manual fix: stop the server, delete
`remotion-composer\build` and `remotion-composer\.build.tmp-*`, restart. See the
original doc (`WINDOWS_REMOTION_WINERROR5.md`) for the full manual runbook,
including the one-time Windows Developer Mode / `npm install` setup notes it
covers that aren't part of this code fix.

## Rule 13 — detection ≠ resolution: when an LLM retry keeps failing the same finding, replace it with a deterministic guarantee, and apply that guarantee on EVERY path that can produce the final props

Confirmed real bug (2026-07-17, user directly flagged it from a rendered screenshot,
angrily, after being told it was "fixed"): `intro_lead_dead_space` (Rule 9) fired
correctly in the props_lint retry loop on three separate backtest runs — the
*detection* never failed once — but the LLM's own replan attempt never once
resolved it in three rounds. The delivered video still shipped with the exact
5.2s dead zone Rule 9 was built to catch. Two more real defects turned up in the
same investigation: a full-canvas section takeover with neither `timeline` nor
`icon` (`section_takeover_lacks_content`, same visual signature as Rule 4 but a
different root cause — this time content_planner just never attached either to
this chapter, not a dark-mode lockout), and a speaker-card shrink transition
tied to *whenever the first real content happened to be ready* instead of to
when the intro actually ended, leaving the card sitting full-size and idle for
5+ seconds regardless of what content_planner tried.

**Four fixes, landed together, same investigation:**

- **Fix C14** (`_build`, the `intro` branch's mode_schedule clamp): the existing
  code already clamped a workflow-transition that started *too early* (before
  the intro finished) — pushing it later. It had no clamp in the other
  direction. Added a symmetric cap: the *first* workflow transition can't start
  later than `introOutFrame + _MAX_DOMINANT_HOLD_FRAMES` (100), regardless of
  when content_planner's own first content item happens to mount. Only the
  first transition is capped — later dominant/workflow alternation is real
  content-driven timing and must not be touched.
- **Fix C13** (`_fill_intro_lead_dead_space`): if `intro_lead_dead_space`
  survives the full 3-round LLM retry loop, deterministically insert one
  lightweight `topicCard` covering the gap — headline text taken verbatim from
  whatever caption is actually being spoken in that window (zero invention),
  mount frame computed from the *real* `scenes` transition windows
  (`_transition_windows`, the same function `element_mounts_during_card_transition`
  uses) rather than a guessed fixed offset — an early version used `gap_start +
  20` and that fixed offset landed the card inside a still-transitioning card,
  producing a *worse* result the safety valve correctly caught and rejected.
  Guarded by a lint-before/lint-after safety valve that rejects the insertion
  if it introduces any finding *type* that wasn't already present — comparing
  raw counts alone isn't enough (verified: a real regression traded
  `intro_lead_dead_space` for two different new findings at an unchanged total
  count of 5, which a count-only check would have silently accepted).
- **Fix C15** (`_demote_content_free_takeovers`): if `section_takeover_lacks_content`
  survives the retry loop, deterministically demote it — remove the `sections`
  entry AND the matching speaker-hide `opacityKeyframes` pair (removing only
  one leaves the *worse* state of "speaker still hidden, decorative blob also
  gone," an empty canvas with nothing on it at all). This is the check's own
  third suggested remedy ("this content isn't worth a full-canvas takeover"),
  chosen over synthesizing a fake `timeline`/`icon` because the section's real
  content (a `stepList`) is *already* independently anchored in the content
  zone across the same span — the takeover was adding nothing but a blob.
  Same lint-based safety valve as C13.
- **Fix C16** (the real catch, found while verifying C13/C15 rather than just
  trusting them): both deterministic guarantees were wired in *only* right
  after the main props_lint retry loop. But `_op_apply_style` has a **second**
  path that can produce the final `props` — when `qa_stills`' vision review
  finds a "high" severity issue, it calls `props = _build(feedback=...)`, a
  completely fresh content-planning call that **never passed through C13/C15
  at all**. This is exactly why the first "verified" render still showed the
  blob: vision QA caught the still-open dead-space gap as "空画布" (empty
  canvas, high severity), triggered a replan, and that replan's output shipped
  raw. Fixed by extracting both guarantees into one `_apply_deterministic_guarantees`
  closure and calling it after *every* path that can yield final props — the
  main loop's `best_props` AND the vision-QA replan's `_build()` output — with
  an explicit `props_path.write_text(...)` re-write after the second call site
  too (same "disk and memory can desync" lesson as Fix C9 — `_build()` already
  unconditionally writes an un-guaranteed version to disk; that write must be
  overwritten with the guaranteed one, not trusted).

**Verified end-to-end, not just at the props level:** for both C13/C14 and
C15/C16, the sequence was: make the change → test the specific function
directly against the real job's captured props/findings → confirm via a fresh,
complete `_op_apply_style()` re-run (real transcription fallback, real
content_planner LLM calls, real props_lint loop, real Remotion render) → pull a
frame directly from the *actual rendered mp4* (not a props-level check, not a
QA still, the literal delivered file) at the location the bug used to occupy →
confirm visually. The C13/C14 first-pass "success" that turned out to still
have the bug (due to the C16 gap) is the reason for insisting on this — a
props-level pass and even a full render can both look clean while the actual
mechanism that would matter for the *next* video never got exercised.

**General principle, worth restating from Rule 5 in a new form:** a fix that
lives in only one of several code paths that can produce the same artifact
isn't a fix, it's a fix for *a* path. Before declaring a deterministic
guarantee "wired in," grep for every call site that can produce the value it's
supposed to guarantee something about — not just the one you were looking at
when you wrote it.

## Rule 14 — a QA still can be sampling a frame that was never meant to show anything; check `pick_qa_frames` before assuming content_planner is at fault

Confirmed real bug (2026-07-19, root-caused after the user reported the WhatsApp
"品牌样式渲染这一步没成功…可回复 'retry'" degradation message recurring across
multiple real jobs — `job_ac00838adea9` and `job_1b7254abcd66`, both on
2026-07-17, same signature both times, *after* Rule 13/Fix C16 had already
landed). Rule 13's fix closed the loophole where a deterministic guarantee
wasn't applied to every props path — but these two jobs degraded anyway, and
Rule 13's guarantees (`_fill_intro_lead_dead_space` / `_demote_content_free_takeovers`)
had nothing to do with it. Opening the actual flagged still (`qa_stills/f0.png`,
per Rule 9's own "always open the flagged still" principle) showed a **literal**
blank cream-colored canvas — not "unfilled content zone with real chrome
visible" (Rule 9's case), pure background color, nothing rendered at all.

**Root cause:** `qa_stills.pick_qa_frames`'s Fix C7 logic samples `win_start - 8`
for every scene-transition window, to catch an outgoing element that hasn't
finished clearing before the next one appears. But `scenes[0]` is *always*
`{"frame": 0, ...}` — the SpeakerCard's own initial keyframe — so the first
transition window is always `(0, X)`, and the old code unconditionally added
`max(0, 0 - 8) = 0` to the sample set on *every single job*. Frame 0 is the
instant before the SpeakerCard's entrance animation has rendered anything —
there is no "outgoing content" to verify has cleared, because the video hasn't
started yet. Two things made this worse than a merely wasteful sample:

1. **The retry can never fix it.** `_op_apply_style`'s vision-QA retry path
   feeds the finding back into `content_planner` as replan feedback — but
   content_planner has zero control over the SpeakerCard's own entrance
   timing. Every retry regenerates the same frame-0 emptiness, so "retried and
   still found the issue" was guaranteed from the start, not a sign the retry
   budget was too small.
2. **Vision QA's severity call on this exact frame is not deterministic.** The
   same essential frame (background color, SpeakerCard not yet mounted) was
   scored `"无内容" / low` (shipped fine) in `job_localdemowalk` and `"空画布"
   / high` (triggered the retry-then-degrade path) in the two jobs that
   actually degraded. A coin-flip on a meaningless sample was surfacing as an
   intermittent-but-recurring production failure.

**First fix attempt (incomplete — caught by live verification, not a props-level
test):** skip the `win_start - 8` sample when `win_start == 0`. `test_qa_stills.py`
passed immediately. A live `_op_apply_style()` re-run against `job_ac00838adea9`
(no mocking — real re-transcription, real content-planning LLM calls, real
Remotion render, real vision-QA call) then degraded *again*, still citing frame
0 as `"空画布"/high`. Root cause of the miss: `pick_qa_frames` has a **second**,
completely independent rule that can also resolve to frame 0 — the "full-screen
interval midpoint" sampler two blocks above the transition-window loop, which
scans `range(0, duration_frames, 30)` for frames where the card isn't docked and
picks the midpoint of that list. The live re-run's content-planning call
(non-deterministic — a different call than the one that produced the original
bug) happened to generate an intro-collapse transition only 20 frames long
(`scenes[0]` at frame 0 → `scenes[1]` at frame 20, instead of the original run's
180 frames). By frame 30 the card was already docked, so `full_frames` was
`[0]` — a single element — and its "midpoint" (`full_frames[len // 2]`) was
therefore 0 again, through a rule that has nothing to do with
`_transition_windows` and was completely untouched by the first fix.

**Actual fix:** don't special-case individual rules (proven fragile — that's
exactly how the first attempt missed this). Enforce the invariant once, at
`pick_qa_frames`' single exit point: `return sorted(f for f in frames if 0 < f
< duration_frames)`. No rule in this function actually needs frame 0
specifically — the "intro landed" check already samples `introOutFrame + 15`,
well after the entrance — so excluding it centrally closes this path, the
`full_frames` path, and any future rule that might independently land on 0,
instead of requiring every future contributor to remember to guard against it
individually. No change to `props_lint.py`'s own `_transition_windows` usages —
both of those key off `win_end`/mount-time membership, never treat `win_start`
itself as a thing that needs visible content, so they don't share this bug.

**Verified:** `test_qa_stills.py` now covers both paths — the original
transition-window case and a second case reproducing the live run's short
(0→20 frame) intro collapse that drove `full_frames` down to `[0]`. Both
confirm frame 0 is excluded while genuinely useful nearby samples (the
transition's/collapse's end-side, `+8`) are unaffected. This *is* the "verified
end-to-end, not just at the props level" standard Rule 13 calls for — the
second bug was only found because a live re-run was actually attempted rather
than stopping at a green unit test; a props-only "fix" here would have shipped
with the exact gap that caused the recurrence in production. A live re-run
against the corrected code, to confirm this job actually completes without
degrading, is still the next step (see `docs/CHANGELOG.md` for whether that has
happened yet by the time you're reading this).

**General principle:** when a vision-QA finding keeps surviving retries no
matter what content_planner produces, ask whether the *sample* itself is
content_planner's to control at all before spending another round on replanning.
Check `pick_qa_frames`'s output against the frame index the finding cites — a
finding on a frame that sampling logic (not content) chose to probe is a
sampling bug, not a content bug, and no amount of replanning will ever change it.

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

## Rule 15 — a "final recompute" guarantee is only final if it runs after every step that can still touch the thing it's guaranteeing

Confirmed real bug (2026-07-21, found via `whatsapp_mvp/simulate_job.py` — a
replica harness added specifically to stop diagnosing "brand style failed" by
trial-and-error against the live WhatsApp bot): `props_lint`'s `element_over_card`
and `facecam_hidden_too_long` kept recurring identically across independent
`content_planner` replans on the same real job (`job_452ef6c48100`), never
resolving no matter how many LLM rounds ran.

**Two compounding bugs, found in order:**

1. **Fix C33 (2026-07-20) only recomputed `scenes` once, inside `_build()`.**
   But `_apply_deterministic_guarantees` runs C13/C15/C37 *after* `_build()`
   returns, and any of them can add or modify content-zone elements (C13
   inserts a new `topicCard`) without triggering another recompute. The frozen
   `scenes` stopped matching reality the moment C13 fired. **Fix C38**:
   extracted the recompute into a standalone `_recompute_scenes_from_content()`
   (reads `props["dataCards"]`/`["gauges"]`/etc. — public JSON keys, not
   `_build()`'s local variables — specifically so it's callable from anywhere),
   and calls it unconditionally at the true end of the guarantee chain,
   regardless of which guarantee fired.

2. **The actual root cause, one layer deeper — `_insert_transition_holds`
   (Fix C21, 2026-07-17) has been wrong since it was written.** It treats
   card-*shrinking* and card-*growing* transitions identically: given two
   scenes keyframes far apart with different sizes, it always inserts an
   early "arrival at the *next* keyframe's size" a fixed `_TRANSITION_HOLD_
   FRAMES` (20 frames) after the *first* one. That's correct for shrinking
   (Dominant → Workflow: get the oversized card out of the way as soon as
   possible). It is backwards for growing (Workflow → Dominant): a `workflow`
   entry exists in `mode_schedule` *because* real content needs that screen
   space for a while — arriving early at the bigger (Dominant) size ~20
   frames after entering `workflow` mode makes the card grow back to full
   size while the content it's supposed to make room for is still on
   screen. This is the actual mechanism behind every "content-zone element
   overlapping an oversized facecam" bug this file has chased (Rule 4's
   dead-space cases aside) — C31/C33/C38 were all correctly re-deriving
   `scenes` from `mode_schedule`, but `_insert_transition_holds` was
   corrupting the *conversion* on the growing side every time. **Fix C39**:
   direction-aware — shrinking keeps the original behavior (arrive early
   at the smaller size, hold there); growing now holds at the *smaller*
   (`prev`) size and only arrives at the bigger size `_TRANSITION_HOLD_
   FRAMES` *before* the next real transition, not after the previous one.

**Why this took so long to find**: `_workflow_mode_schedule`'s own event-scan
(Fix C31's dependency) was completely correct — feeding it the real ranges by
hand always produced the right schedule. The bug only appeared after
`_mode_schedule_to_scenes` converted that correct schedule into `scenes`,
inside a helper (`_insert_transition_holds`) three call-frames away that
nobody suspected because its *own* fix (C21) was already validated and had
looked correct for years of "shrinking" cases — it was simply never exercised
on a real "growing after a long workflow hold" case until this specific
video's content shape hit it.

**General principle, worth restating from Rule 13 in a new form:** when a
"final guarantee" recompute keeps failing to hold, don't just add more
guarantees on top — check every function *between* the schedule and the
rendered geometry for an assumption that only holds in one direction. A
correct scheduler feeding a broken converter still produces broken output,
and the converter can look validated forever if nobody happens to test the
direction it's wrong in.

## Where the rest of the story lives

- `docs/STATUS.md` — architecture baseline, branch status (predates most of the
  fixes above — treat as historical, not current).
- `docs/CHANGELOG.md` — dated entries.
- `contracts/README.md` — the frozen op_registry/render_props/style_params shapes.
