# WhatsApp × OpenMontage Editing System — Consolidation & 3-Person Split

## 0. Goal (one sentence)

A user uploads a **source video** on WhatsApp (plus an optional **text description**
and/or **example video**); the backend edits it using OpenMontage's pipeline and
tools; in the middle, an **L2 governed agent** understands the request and produces
an edit plan; a built-in **XiaojinEditorial** template guarantees that even with
**zero instructions** the user gets a good-looking vertical result.

## 1. Confirmed decisions

1. **Example video** → extract **quantifiable style parameters** (palette / caption
   position / aspect / pacing / speaker framing) and map them onto the template's
   props. No pixel-level replication.
2. **Trunk architecture** = the **L2 governed line** (agent reads manifest/skill +
   tool-calling + self-review + schema); **port** postxhs's capabilities into it.
3. **Built-in template** = **XiaojinEditorial (only one)**; retire ReferenceStyleEdit
   and PostXhsEditorial.

---

## 2. Current state — reusable assets (spread across three branches)

| Asset | Where it lives now | Status | Target home |
|---|---|---|---|
| L2 agent (manifest/skill governance + tool-calling + reviewer + schema) | local `whatsapp-connection` | ✅ works | **trunk** |
| WhatsApp gateway (Node webhook + Python API + queue + confirm/render/revise) | local | ✅ | trunk |
| op→handler registry + 9 base edit operations | local | ✅ | trunk |
| This session's fixes (trim_leading_silence bug, adjustable silence threshold, confirm-timeout backgrounding) | local (on disk) | ✅ | trunk |
| In-chat revision (Phase B) | local | ✅ | trunk |
| T1 transcript-awareness (feed transcript to agent for content-level selection) | container working copy | 🟡 half-done | **fold into P1** |
| `content_planner` (chapters + data cards + word-level filler judgment) | `postxhs` branch | ✅ but wired to L1.5 | **P2 port** |
| `_op_remove_filler` / `_op_apply_style` handlers | `postxhs` branch | ✅ handler layer reusable | **P2 port/adapt** |
| Gateway `/files` proxy (one tunnel serves downloads too) | `postxhs` branch | ✅ | **P3 port** |
| Real-traffic e2e fixes (duration calculateMetadata, SpeakerCard interpolation) | `postxhs` branch | reference | P3 borrow |
| XiaojinEditorial template + xiaojin components + style YAMLs | `video-studio-style-integration` | 🟡 unwired / not build-verified / carries uv-init noise | **P3 owns** |

---

## 3. What still needs building (gaps, in three blocks)

**A. Orchestration layer (L2 agent)**
- Add `remove_filler` and `apply_style` to the agent tool vocabulary
  (`ALL_TOOL_SCHEMAS`/`OP_TOOLS`) + manifest whitelist + reviewer/schema.
- Finish T1: feed the timestamped transcript to the agent so it can do
  **content-level selection** ("keep the part where I talk about X", "cut the
  tangent about Y").
- Implement the "**zero instruction → template result**" default plan
  (default `remove_filler` → `apply_style`).
- Consume the "example-video style params" so the agent folds them into the plan
  and the template props.

**B. Backend capabilities (Python handlers, planner-agnostic)**
- Port `content_planner` (`plan_content` for chapters/data cards,
  `plan_filler_removal` for word-level filler).
- Port `_op_remove_filler` (word-level keep-range concat).
- Change `_op_apply_style` to render **XiaojinEditorial** (not PostXhs), building
  XiaojinEditorial props from (content plan + captions + style params).
- **Build a new "example video → quantifiable style params" extractor** (using
  OpenMontage's `frame_sampler` / `scene_detect` / `face_tracker` / color analysis:
  palette, aspect, caption position, pacing, `objectPosition`, etc.).

**C. Template + gateway (TS/Node)**
- XiaojinEditorial: remove the uv-init noise + **build-verify** (install deps,
  `tsc --noEmit`, do a real render).
- Fix duration/caption misalignment; make `calculateMetadata` derive length from
  `durationSeconds` (borrow the postxhs fix).
- **Add a data card (InfoCard equivalent) to the xiaojin component set** — otherwise
  content_planner's data-card plan has nothing to render.
- Define and freeze the **render props schema** (P2 builds props against it).
- Port the gateway `/files` proxy so one tunnel serves both the webhook and file
  downloads.

---

## 4. Branch consolidation (untangle the mess first)

1. Create trunk branch **`whatsapp-studio`** off the local L2 line (commit this
   session's fixes + T1 cleanly first).
2. Each person works on a feature branch → PR into trunk:
   - `feat/agent-orchestration` (P1)
   - `feat/pipeline-capabilities` (P2)
   - `feat/template-and-gateway` (P3)
3. **Retire** the two drifting branches (cherry-pick what's needed, then stop
   maintaining them in parallel):
   - `video-studio-style-integration` → cherry-pick XiaojinEditorial + xiaojin
     components into P3; drop the `main.py`/`pyproject.toml` uv-init noise, the
     unrelated font-loading change, and the other two compositions.
   - `whatsapp-connection-postxhs-content-planning` → cherry-pick
     `content_planner` / `remove_filler` / `_op_apply_style` handler / `/files`
     proxy into P2/P3; drop the PostXhs composition, L1.5-only changes, and the
     stray `body.json`.

---

## 5. The 3-person split (parallel, each on their own machine)

### P1 — Agent orchestration & trunk integration (Python / LLM)
**Files**: `whatsapp_mvp/agent_editor.py`, `worker.py`, `webhook.py`,
`pipeline_defs/talking-head.yaml`, `schemas/`
**Tasks**
- Wire `remove_filler`/`apply_style` into the L2 tool vocabulary + manifest
  whitelist + reviewer checkpoints.
- Finish T1: feed transcript to the agent, enabling content-level selective editing.
- Implement "zero instruction → template result" default plan; fold the
  example-video style params into the plan and props.
- **Trunk & contract owner**: maintain `whatsapp-studio`, lead defining the 3
  contracts below, own final integration.
**Start with**: produce the 3 contract JSON schemas + fixtures, hand to P2/P3.
**Interface out**: emits a list of ops; calls handlers by op name + params.
**Depends on**: P2's handlers callable by name, P3's props schema — **use
fixtures/stubs to run in parallel; not blocked.**

### P2 — Pipeline capabilities & example analysis (Python / video tooling)
**Files**: `whatsapp_mvp/pipeline_runner.py` (handlers), `content_planner.py`,
**new** `whatsapp_mvp/reference_analyzer.py`
**Tasks**
- Port `content_planner` (chapters/data cards + word-level filler),
  `_op_remove_filler`.
- Change `_op_apply_style` to build XiaojinEditorial props per **contract ②** and
  invoke P3's render entry.
- **Example-video extractor** (this group's biggest new work): example video /
  screenshot → quantifiable style params (**contract ③**), using OpenMontage
  analysis tools.
**Start with**: port `content_planner`/`remove_filler` first (no external deps,
runnable + unit-testable immediately).
**Depends on**: P3's render props schema — write `_op_apply_style` against the
contract ② fixture.

### P3 — Template & gateway (TS / Node)
**Files**: `remotion-composer/*`, `server/*`
**Tasks**
- XiaojinEditorial: strip noise + install deps + `tsc --noEmit` + real render
  (against the David demo fixture).
- Fix duration/caption alignment, dynamic `calculateMetadata`, `objectPosition`
  as an input.
- Add a data card (InfoCard equivalent) to the xiaojin component set.
- Freeze the **render props schema (contract ②)**; port the `/files` proxy.
- Provide a stable render entry: `npx remotion render XiaojinEditorial --props=<json>`.
**Start with**: **can start first, no dependencies** — get "compiles + renders"
working with the David demo props.
**Depends on**: nothing hard (style params / content plan all arrive via props; use
fixtures first).

---

## 6. The key to real parallelism: freeze 3 contracts on Day 0 (~half a day)

| Contract | Contents | Owner | Consumer |
|---|---|---|---|
| ① Op registry | op names + param JSON for `remove_filler`/`apply_style` (and all ops) | P1 | P2 implements handlers |
| ② Render props | the props JSON XiaojinEditorial consumes (videoSrc/durationSeconds/colorMode/speakerObjectPosition/scenes/chapters/captions/dataCards/compliance…) | P3 | P2 produces |
| ③ Style params | quantifiable params extracted from the example video (palette/aspect/captionPosition/pacing…) | P2 | P1 folds into plan |

After freezing, each person commits a **fixture file** and develops against the
fixtures independently; integrate at the end.

## 7. Integration order (bring-up)

P3 renders independently (fixture props) ✅ → P2 handler produces matching props and
successfully calls P3's render ✅ → P1 agent emits correct ops chaining
remove_filler/apply_style ✅ → WhatsApp end-to-end.
