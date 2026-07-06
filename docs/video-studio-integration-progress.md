# video-studio → OpenMontage Style Integration — Progress

> Branch: `video-studio-style-integration`, branched from `main`.
> Does NOT touch `whatsapp-connection` or `wa-montage` — no files under
> `whatsapp_mvp/` or `server/` are modified by this branch, intentionally,
> so it can be reviewed/merged independently of that work.

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
- **No pipeline wiring.** Nothing in `pipeline_defs/talking-head.yaml`'s
  `compose` stage or `whatsapp_mvp/pipeline_runner.py` invokes this
  composition. Today, requesting `xiaojin-editorial` style via the WhatsApp
  MVP branch would not produce this output — that branch's `pipeline_runner.py`
  only exposes trim/reframe/caption-burn operations, no style-rendering step
  at all (see the assessment that motivated this branch).
- **`daja-default` has no composition.** Only the style tokens were ported.
  Video-studio itself never had a from-scratch daja-default Remotion build
  to port from (its reference edits were pre-Remotion ffmpeg overlays).
- **Not build-verified.** `remotion-composer/node_modules` was not installed
  and `npx tsc --noEmit` / a real Remotion Studio preview were not run
  against these changes. The component set was written carefully against
  the existing prop/import conventions in this codebase, but has not been
  compiled or rendered. Do this before relying on it.
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
- **video-use's transcript-judgment filler removal was not ported.** The
  WhatsApp MVP branch's `remove_silences` operation uses OpenMontage's
  `SilenceCutter` tool, which is threshold-based silence detection, not
  semantic judgment over a transcript. This branch doesn't touch that either
  way — it's a pipeline-runner concern, out of scope for a styles-only branch.

## Suggested next step for whoever picks this up

1. Install `remotion-composer` deps and actually render `XiaojinEditorial`
   with its default props — confirm it compiles and looks like the intended
   style before anything else.
2. Decide `ReferenceStyleEdit` vs. `XiaojinEditorial` (see above) — don't
   let both continue evolving independently.
3. Only then consider wiring either into `whatsapp-connection`'s
   `pipeline_runner.py` as a new `compose` operation — that's a separate,
   larger task than this branch, and should probably be its own branch too.
