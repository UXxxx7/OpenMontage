# WhatsApp × OpenMontage — Project Status (single source of truth)

> Last updated: 2026-07-07 | Trunk: `whatsapp-studio`
> This file is the one authoritative status. The old `whatsapp-mvp-progress.md` (early codex leftover) and per-round snapshots are **retired** — this file wins.
> Companion docs: `docs/L1.5-vs-L2.md` (planner lane), `contracts/` (the 3 contracts), `whatsapp_mvp/CLAUDE.md` (working conventions).

---

## 0. Current state in one line

**We can plan and execute "cleanup" edits (trim leading silence / filler / pauses / content-based selection / subtitles / vertical) end-to-end and deliver a finished cut. "Render a branded template (apply_style)" can be planned, but the real render is blocked on P3's template not being ready.**

---

## 1. Architecture baseline (settled — do not drift)

- **Planner = L2** (`agent_editor.py`). `worker` runs L2 as primary; `llm_planner` (L1.5) is frozen, fallback-only, no longer extended.
- **Execution = op→handler registry** (`pipeline_runner.py`), planner-agnostic.
- **Three contracts** frozen: `op_registry` / `render_props` / `style_params`; changing a contract needs P1/P2/P3 three-way sync.
- **OpenMontage integration path**: agent reads the manifest (via `lib.pipeline_loader`, schema-validated) + edit-director/Layer3 skill → calls OM tools; source media first goes through `lib.source_media_review`; reviewer self-audit + confirm checkpoint.

---

## 2. Branch status

| Branch | State | Notes |
|---|---|---|
| `whatsapp-studio` | **trunk** | L2 planning + merged pipeline_runner + contracts + docs; L2 wiring (agent_editor/worker) just committed |
| `feat/pipeline-capabilities` (P2) | active | Reverted L1.5, merged upstream, worker→L2, ported /files proxy, 4 test suites pass; **re-merge once** after P1's L2 wiring lands |
| `feat/template-and-gateway` (P3) | **not created** ⚠️ | **critical-path blocker** — see §5 |
| `feat/integrate-p2` (P1 integration) | temporary | merge working branch |
| `video-studio-style-integration` / `...-postxhs-...` | retired | needed parts cherry-picked, no longer maintained |

---

## 3. Done

**Planning / governance (P1)**
- L2 agent: manifest (lib.pipeline_loader) + edit-director + Layer3, reviewer (review_focus / 2 rounds / auto-fix critical), schema-compliant `edit_decisions`.
- `remove_filler`/`apply_style` wired into the L2 tool vocabulary + zero-instruction default + double-subtitle guard.
- **T1 transcript-awareness** (transcript fed to the agent, content-level selection, multiple remove_segment sorted descending).
- **source_media_review** preflight (lib.source_media_review).

**Execution / render layer (P2)**
- `content_planner` (chapters + count-up data cards + word-level filler `plan_filler_removal`).
- handlers: `remove_filler`, `apply_style` (renders XiaojinEditorial), face_tracker framing calibration, `build_xiaojin_scenes` (beat layout), `build_caption_phrases`, `qa_stills`, safe zones.
- `reference_analyzer` (sample video/screenshot → style_params; warm/dark + aspect, honest low confidence).
- 4 unit suites (content_planner / apply_style_props / scenes / …).

**Gateway / fixes**
- Node gateway + confirm/render/revise; **/files proxy** (ported by P2).
- Fixes: `trim_leading_silence` (the always-no-op bug), `remove_silences` adjustable threshold, confirm 30s-timeout backgrounded, Phase B in-conversation revision.

---

## 4. In progress / TODO

| Item | Who | Depends on |
|---|---|---|
| Commit L2 wiring to trunk + P2 re-merge | P1 | none (doing now) |
| `apply_style` graceful degradation (don't crash on render failure; deliver the edited cut) | P1 | none |
| Wire `resolve_reframe_op` into L2 planning (reference aspect → reframe) | P1 | reference upload |
| L2 consumes reference style_params (colorMode/aspect) | P1 | reference upload |
| Multi-media job model + reference upload / role assignment | P1 (job) + P3 (gateway collection state) | P3 |
| face_tracker calibration real-device verification | P2 | opencv/mediapipe won't install |
| Wire compose-stage OM tools (color_grade / audio_enhance first — safe; avoid eye_enhance, cv2 dependency) | P1/P2 | none |
| Align reference_analyzer with `video-reference-analyst` (VideoAnalyzer/VideoAnalysisBrief) | P2 | TBD |

---

## 5. Blockers

1. **P3 critical path**: `feat/template-and-gateway` not created → XiaojinEditorial not build-verified, `apply_style` real render fails. **This is the single highest-leverage item for moving the project forward.** P3 needs to: create the branch → install remotion-composer deps → tsc + real-render a fixture → consume contract-② props (durationSeconds/dataCards/beat scenes).
2. **face calibration**: opencv/mediapipe won't install over the local network; calibration is written but untested on real footage.

---

## 6. What you can test now

- ✅ **Cleanup edits** (testable end-to-end, and you can feel the improvement): "trim the silent opening" (trim fix), "cut the uh/um / filler / retakes" (`remove_filler`, new), "remove pauses for flow", "keep only the part where I talk about X" (T1), go vertical, add subtitles.
- ❌ **Template render (apply_style)**: blocked on P3.
- ⚠️ **Gotcha**: a zero-instruction "just edit it for me" and words like "make it nice / xiaohongshu / branded" trigger apply_style → real render fails → job errors. For testing, give a concrete edit instruction, or land apply_style graceful degradation first.
- Before testing, restart the Python worker + Node worker; needs a DeepSeek key + faster-whisper.

---

## 7. Decision log

- Planner **standardized on L2**, L1.5 frozen.
- Built-in template **sole = XiaojinEditorial**.
- Sample video → **quantifiable style params**; MVP reference controls only **colorMode + aspect** (doesn't touch contract-②/P3).
- Multi-input: **images are always reference; 1 video = target (no ask); 2+ videos = ask role**.
- Contracts frozen; changes need three-way sync.

---

## 8. OpenMontage integration degree (brief)

- **Breadth**: 1/12 pipelines (talking-head), ~10/88 tools, 3/7 artifacts (source_media_review/script/edit_decisions). Still a "talking-head post-production" slice.
- **Fidelity**: within that slice it's a genuine manifest/lib/skill-driven agent producing schema artifacts (not a thin shell).
- **Gaps**: preflight/provider-menu not done; only the edit-director stage skill is read; compose-stage finishing tools (color grade / enhance / scoring / `video_compose` proper render / visual_qa) not wired as ops; apply_style's real render bypasses OM's `video_compose`; reference workflow not aligned with `video-reference-analyst`.
- One line: **wired correctly, not yet wide**. Next deepening = wire compose-stage OM tools + add the later-stage artifacts.
