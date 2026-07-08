# Day-0 Contracts (3) — the starting gun for parallel work

Once these three contracts are frozen, P1/P2/P3 can each develop against the
fixtures independently and integrate at the end.
**Changing a contract requires all three to sync**; as long as the contract holds,
each person's internal implementation is their own business.

## Purpose (why these exist)

A contract is a **frozen agreement on the shape of the data that crosses between the
three workstreams** — the API between teammates. Without it, the work is serialized:
P2 can't build `apply_style` until P3's template exists, P3 can't know which fields
to accept until P2 decides what to emit, P1 can't emit a plan until the handlers
exist. With the shapes frozen on Day 0, each person builds against a stable boundary,
using the other person's **fixture** as a stand-in for their not-yet-built output.
The **schema** is the enforceable spec (validate in CI); the **fixture** is a working
example for isolated testing.

## The three contracts and ownership

| Contract | Files | Owner | Consumer |
|---|---|---|---|
| ① Op registry | `op_registry.schema.json` + `fixtures/edit_plan.example.json` | **P1** | P2 implements handlers by op name |
| ② Render props | `render_props.schema.json` + `fixtures/render_props.example.json` | **P3** | P2 produces, feeds the renderer |
| ③ Style params | `style_params.schema.json` + `fixtures/style_params.example.json` | **P2** | P1 folds into the edit plan |

## Global conventions (everyone must follow)

- **FPS = 30.** Anything named `*Frame` / `atFrame` / `scenes[].frame` is in
  **frames**; `captions[].startMs/endMs` is **milliseconds**; `*_seconds` is
  **seconds**. P2 owns the seconds→frames conversion inside the handlers.
- **`speakerObjectPosition` and `scenes` geometry come from the SOURCE video, not
  the example video.** Per xiaojin's quality rule, speaker framing must be
  **calibrated per source video** (P2 runs `face_tracker` on the source); the example
  video only decides **aesthetics** (palette/caption look/aspect/pacing), not face
  position. That's why contract ③ has **no** objectPosition — don't put it there.
- **content_planner's output needs a light adaptation for Xiaojin**: PostXhs's
  `mode_schedule` (dominant/workflow) does not exist in Xiaojin (Xiaojin schedules
  card position via `scenes`); `info_cards` must be mapped into contract ②'s
  `dataCards`. This is P2's job.
- **`durationSeconds` drives `calculateMetadata`**: Xiaojin currently hardcodes
  length; P3 must switch it to derive from `durationSeconds` (borrow the postxhs
  fix), or long videos crash.
- With optional fields omitted, the renderer must still produce a plain-but-correct
  result (chrome + captions) from the required fields alone — it must not error just
  because intro/outro/dataCards are missing.

## Integration order

P3 renders `render_props.example.json` to get "compiles + renders" working → P2 uses
`style_params.example.json` + a transcript to produce props matching `render_props`
and successfully call P3's render → P1 emits a plan matching `op_registry` chaining
remove_filler/apply_style → WhatsApp end-to-end.
