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
