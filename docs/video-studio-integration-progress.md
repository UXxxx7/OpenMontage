# video-studio → OpenMontage Style Integration — Progress

> Branch: `video-studio-style-integration`, branched from `main`.
> Does NOT touch `whatsapp-connection` or `wa-montage` — no files under
> `whatsapp_mvp/` or `server/` are modified by this branch, intentionally,
> so it can be reviewed/merged independently of that work.
>
> **2026-07-07 update:** this history continues on `feat/template-and-gateway`
> (P3's branch), which implements contract② and adds a `server/index.js`
> files-proxy route. See "Update 2026-07-07" below — that section documents
> the new work; everything above it describes the original port as shipped.

## What this branch actually is

A port of [VeLL-lab/video-studio](https://github.com/VeLL-lab/video-studio)'s
two style presets (`xiaojin-editorial`, `daja-default`) into OpenMontage's
own conventions — style playbook YAMLs under `styles/`, and a real Remotion
composition under `remotion-composer/` for the one that had a working
reference build to port from.

**This branch provides style assets. It does not wire them into an
automated pipeline.** There is no code here that connects this composition
to `pipeline_defs/talking-head.yaml`'s `compose` stage, to the WhatsApp MVP's
`pipeline_runner.py`, or to any agent tool-calling loop. That connective work
is a separate, non-trivial task — see "What's NOT done" below.

## What's included

| Item | Path | Status |
|---|---|---|
| Speaker card (floating, never full-bleed) | `remotion-composer/src/components/xiaojin/SpeakerCard.tsx` | Ported, generalized to take scene schedule as props |
| Chapter nav bar | `remotion-composer/src/components/xiaojin/ChapterNav.tsx` | Ported, generalized |
| Compliance strip | `remotion-composer/src/components/xiaojin/ComplianceBar.tsx` | Ported, generalized |
| Rainbow progress bar | `remotion-composer/src/components/xiaojin/RainbowProgressBar.tsx` | Ported as-is (was already generic) |
| Karaoke captions | `remotion-composer/src/components/xiaojin/Captions.tsx` | Ported, using the direct-lookup method video-studio's own CLAUDE-v2.md documents as the fix for a real CJK sync bug in `createTikTokStyleCaptions` |
| Shared theme tokens (warm/dark) | `remotion-composer/src/components/xiaojin/theme.ts` | New — generalizes what was a single hardcoded palette in the source project |
| Content zone (graphic side opposite the card) | `remotion-composer/src/components/xiaojin/ContentZone.tsx` | Ported as a generic beat-driven slot — see its doc comment for why the per-project section content itself (calendars, quote cards, etc.) was NOT ported |
| Intro title card ("Pattern 2" dark open) | `remotion-composer/src/components/xiaojin/IntroTitle.tsx` | Ported, generalized. The other 3 intro patterns from CLAUDE-xiaojin-editorial.md (stats hook, title+atmosphere, straight-in-with-chips) are not ported |
| Brand strip (non-regulatory) | `remotion-composer/src/components/xiaojin/BrandBar.tsx` | Ported, generalized. Counterpart to ComplianceBar — use exactly one of the two |
| Outro CTA section | `remotion-composer/src/components/xiaojin/OutroSection.tsx` | Ported, generalized. vell-renewal-reminder's QR-code/contact-card outro variant was NOT ported (business-data-driven, needs its own component against a real fact-sheet schema) |
| Composition wrapper | `remotion-composer/src/XiaojinEditorial.tsx` | New, assembles all of the above; `contentBeats`/`intro`/`outro`/`brand` are optional so a chrome-only build still works |
| Registered in Root.tsx | `remotion-composer/src/Root.tsx` | `id="XiaojinEditorial"`, default props reuse the same David/Pacific Life demo script already present in `WhatsAppReferenceEdit` |
| Style playbook | `styles/xiaojin-editorial.yaml` | New, translated from `CLAUDE-xiaojin-editorial.md` into OpenMontage's playbook schema |
| Style playbook | `styles/daja-default.yaml` | New, translated from `CLAUDE-daja-default.md` — **no composition ported for this one** (see below) |
| Pipeline registration | `pipeline_defs/talking-head.yaml` | Added both to `compatible_playbooks` |

## Important: this creates a second, more faithful implementation of the same reference video

`main` already has `remotion-composer/src/ReferenceStyleEdit.tsx` (composition
id `WhatsAppReferenceEdit`), which independently approximates the same
David/Pacific Life source footage — but as a generic, loosely-styled
16:9 landscape treatment (soft cream backdrop, muted gray panels), not the
documented xiaojin-editorial spec (vertical 9:16, exact terracotta/cream
palette, floating card with specific shadow/border treatment, persistent
ChapterNav, ComplianceBar, karaoke captions).

**Both compositions are kept, deliberately not merged.** They encode two
independent, divergent interpretations of the same brief — exactly the kind
of style drift this whole cross-project effort has been trying to catch. If
you're deciding which one to build on:
- `WhatsAppReferenceEdit` — approximate, landscape, no prior validated build
- `XiaojinEditorial` — literal port of video-studio's actual validated
  9:16 build (`motion/vell-renewal-reminder/`), scene positions and
  `objectPosition` calibrated for that specific source video

Reconcile these before either gets wired into a real pipeline — shipping both
independently risks a third, further-diverged variant appearing later.

## Update 2026-07-07 — Contract② implementation

Continues this branch as `feat/template-and-gateway`, implementing the P3 side
of the Day-0 contracts plan (`contracts/README.md`): contract② (render props)
is now enforced, not just documented.

| Item | Path | Status |
|---|---|---|
| Contract② validation | `XiaojinEditorial.tsx` | `ajv` compiles `contracts/render_props.schema.json` and validates props inside `calculateXiaojinEditorialMetadata`, before any frame renders — malformed props now fail loudly instead of rendering blank/`undefined` text |
| Duration-driven frame count | `XiaojinEditorial.tsx` | `calculateXiaojinEditorialMetadata` derives `durationInFrames` from the required `durationSeconds` prop (ported fix from postxhs); replaces the previous hardcoded `Math.ceil(44.9 * 30)` in `Root.tsx`, which would have silently truncated/looped any video of a different length |
| Data cards | `components/xiaojin/InfoCard.tsx` (new) | Count-up stat cards for contract②'s `dataCards` field — the "[P3 NEW capability]" the schema already reserved space for |
| Gauge / countdown / calendar / QR components | `components/xiaojin/{RiskGauge,CountdownRing,Calendar,QRContactCard}.tsx` (new) | Ported from `vell-renewal-reminder`/`vell-renewal-fresh`'s bespoke sections, generalized to props. Ported bugfixes along the way: `Calendar` now computes a real month grid instead of a hardcoded July-2025 array; `RiskGauge`'s needle direction was fixed to track the same left→right sweep as its own fill (previously swung opposite to it) |
| Schema fields for the above | `contracts/render_props.schema.json` | Added `gauges` / `countdowns` / `calendarEvents` / `qrContact` — **not yet wired into `XiaojinEditorial.tsx`'s render tree.** Same incremental scoping `dataCards`/`InfoCard` used: schema + component first, render-tree wiring is a separate follow-up |
| Chapter field rename | `components/xiaojin/ChapterNav.tsx` | `at`/`zh`/`en` → `atFrame`/`label`/`labelEn` to match contract② exactly (`labelEn` now optional, for non-bilingual builds) |
| Speaker card motion fix | `components/xiaojin/SpeakerCard.tsx` | A bare multi-keyframe `interpolate()` over widely-spaced `scenes` was gliding continuously across the entire gap between two keyframes, not holding-then-transitioning like `compose-director.md` specifies. Fixed by inserting a hold keyframe 20 frames before every real scene change |
| Paint order fix | `XiaojinEditorial.tsx` | `SpeakerCard` now renders first, so `contentBeats`/`dataCards`/`outro` paint on top of it — previously they painted first and were invisible behind an opaque Dominant-mode card (confirmed via render test) |
| Build verification | — | `npx tsc --noEmit` now actually runs against this component set (previously undone — see below). No errors from any xiaojin/contract file; 19 pre-existing errors remain elsewhere in the repo (`ProviderChip.tsx`, `Explainer.tsx`, `Root.tsx`'s other compositions), unrelated to this work and not introduced by it |

**Still not done:** the four new schema fields (`gauges`/`countdowns`/`calendarEvents`/`qrContact`) have components and schema but nothing in `XiaojinEditorial.tsx` reads or renders them yet — a caller supplying them today would have them silently ignored (ajv allows omission since they're optional, but doesn't get them on screen). Wire them into the render tree the same way `dataCards`/`InfoCard` was done, then re-run `tsc` + a real render before relying on any of them.

## What's NOT done (accepted gaps, not oversights)

- **Remaining components, not ported (bespoke, not reusable templates):**
  every video-studio motion project also has its own one-off content
  graphics — `CalendarGraphic`/`ShieldGraphic`/`RenewalSection`/
  `CoverageSection`/`LapseSection`/`ContactSection` (vell-renewal-reminder),
  `QuoteSection` (chris-quote), `CollectionSection`/`OriginSection`/
  `RevealSection` (iman-watches), `CTAGraphics`/`MoneyRedirect`/
  `PayoutGraphics`/`TaxDeduction`/`AgentCredential` (retirement-fund). These
  are inherently tied to one video's specific business content (an
  insurance calendar, a watch collection reveal, a tax-deduction graphic) —
  porting them as "xiaojin" library components would mean generalizing
  business content that was never meant to be generic. Use `ContentZone`'s
  `beats` prop to supply your own equivalents per project instead.
- **Only 1 of 4 documented intro patterns has a component** (`IntroTitle` =
  Pattern 2). Stats-hook, title+atmosphere, and straight-in-with-chips
  (Patterns 1, 3, 4 in `CLAUDE-xiaojin-editorial.md`) have no component yet.
- **No pipeline wiring in `main`.** Nothing merged yet invokes this
  composition from `pipeline_defs/talking-head.yaml` or `whatsapp_mvp/pipeline_runner.py`.
  **Update 2026-07-07:** a sibling, not-yet-merged branch
  (`feat/pipeline-remove-filler-apply-style`, P2's continuation of
  `feat/pipeline-capabilities`) now adds an `apply_style` operation to
  `pipeline_runner.py` that builds contract② props and calls
  `npx remotion render XiaojinEditorial` — but the two branches have not been
  integration-tested against each other yet (P2 was built against this
  branch's contract② schema, not against a merged `main`). Reconcile/merge
  both before assuming end-to-end wiring works.
- **`daja-default` has no composition.** Only the style tokens were ported.
  Video-studio itself never had a from-scratch daja-default Remotion build
  to port from (its reference edits were pre-Remotion ffmpeg overlays).
- **Not render-verified.** **Update 2026-07-07:** `npx tsc --noEmit` now runs
  clean against the xiaojin/contract file set (see above), but a real
  Remotion Studio preview / actual render of `XiaojinEditorial` with real
  props has still not been done. Compiling is not the same as looking right
  on screen — do a real preview/render before relying on this.
- **Scene schedule and `objectPosition` in `Root.tsx`'s default props are
  copied from video-studio's specific calibration for one specific source
  video.** Per `SpeakerCard.tsx`'s own doc comment (ported from video-studio's
  CLAUDE-v2.md §6a): never reuse a fixed `objectPosition` across different
  source videos. A new source video needs its own calibration pass.
- **The 8-criteria QA rubric from video-studio's CLAUDE-v2.md was not
  ported as an automated check.** It exists there as a prompt for an agent
  to self-score against, not as code. `styles/xiaojin-editorial.yaml`'s
  `quality_rules` list captures the same rules as text, but nothing in this
  branch executes them automatically — that would need a `visual_qa`-style
  tool extension, not a style playbook change.
- **video-use's transcript-judgment filler removal was not ported here.**
  Out of scope for this styles-only branch either way. **Update 2026-07-07:**
  this now exists on the sibling `feat/pipeline-remove-filler-apply-style`
  branch as a new `remove_filler` operation (LLM reads the transcript and
  judges filler/retakes, complementing `remove_silences`'s pure silence
  detection) — see that branch's `docs/whatsapp-mvp-progress.md`.

## Suggested next step for whoever picks this up

1. ~~Install `remotion-composer` deps and actually render `XiaojinEditorial`
   with its default props — confirm it compiles~~ Partially done: deps were
   already installed and `tsc --noEmit` passes clean on this file set as of
   2026-07-07. **Still needed:** an actual Remotion Studio preview / render,
   to confirm it *looks* like the intended style, not just that it compiles.
2. Wire `gauges`/`countdowns`/`calendarEvents`/`qrContact` into
   `XiaojinEditorial.tsx`'s render tree — schema and components exist
   (2026-07-07 update above) but nothing reads them yet.
3. Decide `ReferenceStyleEdit` vs. `XiaojinEditorial` (see above) — don't
   let both continue evolving independently.
4. Reconcile this branch with `feat/pipeline-remove-filler-apply-style`
   (P2's `apply_style` operation already targets this branch's contract②
   schema, but the two have never been integration-tested together) before
   merging either toward `main`.
