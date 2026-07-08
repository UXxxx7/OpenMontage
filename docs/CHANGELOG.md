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
