# Corpus extraction acceptance testing — running summary

Companion to `mapping_stage_summary.md` / `verification_stage_summary.md` /
`graphrag_stage_summary.md`. This defines every name used in the corpus-scale
extraction evaluation (2026-07-10): the two test sets, the three pipeline
states, and every file. The layout mirrors the dev/holdout convention:
`data/<set>_gold_set.jsonl`, `data/<set>.postscreened.jsonl`,
`data/<set>_<N>run_<tag>/runN.extracted.jsonl`, where the tag names the
pipeline state that produced the run.

## Full corpus extraction result (deliverable)

Pipeline: `derived_actors`. Extracted 2026-07-12 (GDPR and AI Act run
separately; the AI Act was re-run after a Gemini quota interruption, GDPR was
not affected). Outputs (gitignored): `data/gdpr.extracted.jsonl`,
`data/aiact.extracted.jsonl`.

| | GDPR | AI Act |
|---|---|---|
| input paragraphs | 748 | 1,071 |
| paragraphs covered | 741 | 1,063 |
| extraction records | 987 | 1,549 |
| extractor errors | 0 | 0 |
| schema valid (first-attempt + re-validation) | 100% | 100% |
| content completeness | 97.7% (20 blank slots) | 97.7% (33 blank slots) |
| span grounding (stated content in source) | 98.8% (22 flagged / 1,849) | 98.4% (48 flagged / 3,047) |

Deontic/Definitional/Applicability/NA split: GDPR 640 / 115 / 109 / 123;
AI Act 987 / 214 / 206 / 142.

**Documented limitations (pipeline frozen — not fixed):**
- Uncovered paragraphs are deterministic guard drops (legislator-subject +
  recital self-referential applicability), varying run-to-run with LLM
  stochasticity. One is a guard false-positive: `aiact:art_18/par_3` (a real
  provider obligation dropped because "Union financial services law" appears
  in its subject clause).
- 53 records carry a blank slot (empty predicate/object/definition or
  present-but-empty condition); all machine-identifiable by
  `schema_validity.py`'s content-completeness check. Includes the
  empty-copula-predicate class (e.g. `gdpr:art_84/par_1#s1`).
- ~1.5% of stated content uses a word absent from the source paragraph
  (out-of-source imports: actor names and cross-referenced content pulled from
  neighbouring provisions).
- Subject on snapped records (`subject_inferred_duty_bearer: true`) can be
  mis-attributed when the paragraph names an actor incidentally rather than as
  the duty-holder (the `_infer_duty_bearer` heuristic; ~8 of 22 such GDPR
  records). Machine-identifiable by the flag.

Quality is certified on the gold sets (dev/holdout F1 ≈ 0.929; test2 F1 0.935),
not per-corpus-record. The corpus is populated by that evaluated pipeline; the
limitations above are the measured residual error, bounded and flagged.

## Why this exists

The full corpus (748 GDPR + 1,071 AI Act paragraphs) has no gold set and never
will. Extraction quality at corpus scale is evidenced by acceptance sampling:
draw a seeded paragraph sample, author gold in the holdout format, grade with
the standard harness (`compare_to_gold.py`, `score_extraction.py`). Two
samples were drawn; each caught real defects; each defect batch was fixed
deterministically and evidenced by replays. The frozen dev/holdout numbers
were never affected (proven each time by digit-identical replay scores).

## Pipeline states (the run-directory tags)

A "state" = the deterministic post-pass suite in `extract_min.py` +
`predicate_norm.py`. The LLM stages never changed.

**`frozen`** — the pipeline exactly as evaluated on dev/holdout (the
2026-07-10 corpus freeze). Subject canonicalisation used a 7-keyword role
list, which test1 showed destroying institutional-actor subjects (notified
bodies, judicial authorities, certification bodies; ~348 corpus records
upper bound) by overwriting them with the default duty-bearer.

**`actorkeep`** — subject guard rewritten after the test1 diagnosis: an
~18-stem actor vocabulary; a subject naming any actor is kept; empty or
non-actor subjects snap to a duty-bearer inferred from the paragraph, else
the regulation default. Evidence it changed nothing else: replay over all 10
saved dev/holdout runs = 0 paragraphs changed; scores digit-identical
(COMBINED P .881 / R .984 / F1 .929 / F2 .961). test2 then showed its
weaknesses: the vocabulary was hand-typed (missed the data protection
officer), the inference could pick the wrong named actor, and the default
was wrong for institutional provisions.

**`derived_actors`** — built after the test2 findings: (a) actor vocabulary
derived in code (`_derive_actor_vocab`: base stems ∪ `mapping/vocab/terms.json`
labels whose head noun is an actor category — how the DPO got in); (b)
non-agent subject detector (provable passive patients and
preposition-preceded phrases re-home as `actorkeep` did; any other grounded
subject is kept + flagged, never overwritten); (c) Annex III sub-point
applies_to fix (generic 'AI systems' overridden by the point's own text,
even when labelled STATED; non-generic STATED protected); (d)
'refrain/abstain from' stripped from PROHIBITION/DISPENSATION predicates;
(e) DISPENSATION with an operator-head predicate ('exempt from') flags.
Evidence: §Evidence ledger below.

Regression tests: `test_subject_guard.py` (24 assertions — every test1 and
test2 finding frozen as a test, plus eval-scale behaviours that must not
change), `test_predicate_guards.py`, `test_tag_pass.py`.

## test1 — the diagnostic set

Seed 7, 36 paragraphs, class/modality mix mirroring the holdout gold set.
Drawn before contamination exclusions existed: **4 paragraphs overlap the
dev/holdout inputs** (gdpr:art_33/par_1, gdpr:art_9/par_2/pt_a exactly;
gdpr:art_17/par_3/pt_a, gdpr:art_9/par_2 by family) → **diagnostic only,
never reportable**. It caught the `frozen` subject-guard destruction class.

| file | role |
|---|---|
| `data/test1_sample.json` | 36 paragraph IRIs + seed + contamination note |
| `data/test1.postscreened.jsonl` | extraction input |
| `data/test1_gold_set.jsonl` | authored gold T01–T57, partially adjudicated |
| `data/test1_1run_frozen/run1.extracted.jsonl` | `frozen` extraction (from the corpus run) |
| `data/test1_1run_actorkeep/run1.extracted.jsonl` | `actorkeep` live re-extraction (validated that fix) |

## test2 — the test set (reportable)

Seed 11, 30 paragraphs, 46 gold records (U01–U46, document order).
Exclusions: all dev/holdout input paragraphs + IRI families, all test1
paragraphs + families; two accepted sibling exposures (gdpr:art_6/par_1/pt_e,
gdpr:art_9/par_3). **Its grade against the `actorkeep` run is the reportable
unseen-corpus number** (pending Yoseph's gold review). Provisional: OVERALL
P 0.896 / R 0.977 / F1 0.935 / F2 0.960. It caught the missing DPO, the
wrong-default duty-bearers, the Annex III sub-point gap, meaning-inverting
'refrain from' predicates, and operator-as-predicate dispensations.

| file | role |
|---|---|
| `data/test2_sample.json` | 30 paragraph IRIs + seed + exclusion rules |
| `data/test2.postscreened.jsonl` | extraction input |
| `data/test2_gold_set.jsonl` | authored gold U01–U46, adjudicated by Yoseph |
| `data/test2_5run_actorkeep/run1..5.extracted.jsonl` | **the reported 5-run set** — run1 = the original live run (all U-finding analysis refers to it); runs 2–5 extracted 2026-07-10 from the pinned `actorkeep` commit (4624813) via a git worktree, so the code state is byte-identical |
| `data/test2_1run_derived_actors_replay/run1.extracted.jsonl` | `derived_actors` post-passes replayed over saved run1 (no LLM) |

**Reported numbers (5-run mean [min, max], U12 charged per Yoseph's ruling):
OVERALL P 0.903 [0.896, 0.915] / R 0.969 [0.956, 0.977] / F1 0.935 [0.935,
0.935] / F2 0.955 [0.947, 0.960]. Deontic P 0.932 / R 0.976 (identical in
all five runs). Definitional 1.000. Applicability 0.000 (2 gold records,
both fail — see ledger).** Failure profile is stable across runs: U12 missed
5/5 (modality-twin class, extra PROHIBITION each time), U45 HARD 5/5, U35
fails 5/5 but oscillates between truncated-applicability (runs 1/3/5) and a
class-flip to deontic (runs 2/4) — that savings clause is run-unstable at
the class level; art_39/par_1/pt_e over-split 5/5, art_58/par_5 over-split
3/5. No failure class appears in runs 2–5 that run1's review didn't already
document.

**Decision (Yoseph, 2026-07-10): `derived_actors` is NOT re-tested on a
sample.** Its fixes are deterministic post-passes; their evidence is the
exhaustive replay ledger and the regression tests, and the write-up states
this explicitly ("minor deterministic fixes, not worth another test round").
The only sampled evidence for it is the DPO mechanism probe (below), because
vocabulary-dependent subject retention is the one stochastic behaviour.

## Evidence ledger for `derived_actors` (all deterministic replays, no LLM)

Predicted diff set, stated before running: dev/holdout zero; test1 zero;
test2 exactly {U15 predicate, U26 flag, U45 applies_to}.

Outcome, every deviation root-caused:
- dev 5×0 ✓; test1 `frozen` replay 0 ✓; test2 U15/U26/U45 ✓ (grades moved
  only through U15 + U45: F1 0.935 → 0.946, HARD 2 → 1).
- holdout run5: ONE flag-only change — the dispensation-operator rule caught
  a pre-existing 'exempt from' predicate (gdpr:art_30/par_5#s1). True
  positive; no value changed; scores digit-identical.
- three cosmetic subject rewrites ('the supervisory authority' →
  'supervisory authorities'; test1 `actorkeep` replay ×1, test2 ×2): the
  canonicaliser normalising values the `actorkeep` inference wrote in
  non-canonical form; tables since aligned; same actor, soft field, no grade
  movement.
- test2 art_50/par_1#s1: flag-only, second dispensation-operator true
  positive ('require to ensure').
- U45 required the STATED-generic gate change in `derived_actors`(c): its
  saved value was generic-'AI systems' labelled STATED, so a CONTEXT-only
  gate refused it. Non-generic STATED values remain protected (tested).

## test2 finding ledger (status in the REPORTED run vs the deliverable pipeline)

The reported numbers grade `test2_1run_actorkeep`. Fixes in `derived_actors`
do NOT change the reported numbers; they change the deliverable pipeline.

| finding | in the reported run | in `derived_actors` | fix evidence |
|---|---|---|---|
| U09, U11 — DPO subjects destroyed ('the controller' / 'the supervisory authority') | **wrong; soft layer, invisible to F1 — must be quoted as counted subject errors** | fixed (DPO in derived vocabulary; grounded actors never overwritten) | regression test + DPO mechanism probe (item 2 below) |
| U15 — PROHIBITION + 'refrain from requesting' (meaning-inverting) | wrong | fixed (predicate rewrite) | exhaustive replay: exactly this record changed, grades moved only here |
| U45 — Annex III sub-point generic 'AI systems' scope | wrong (HARD) | fixed (sub-point override) | exhaustive replay: HARD 2→1 |
| U26 — dispensation predicate 'exempt from' (operator, not the relieved duty) | wrong | flagged, value not repaired | replay + unit test |
| U04, U12, U17 — passive patients snapped to wrong duty-bearer | wrong; flagged | unchanged (paragraph never names the right bearer; no grounded rule can reach gold) | counted residual |
| U35 truncation, U44 thin predicate, 2 over-splits | wrong/weak | unchanged | counted residuals |
| U12 modality (PERM-restricted vs PROH-with-exception) | — | — | Yoseph's open gold decision |

## Status and open items

1. Yoseph's test2 gold review — in progress. Its grade vs
   `data/test2_1run_actorkeep/run1.extracted.jsonl` = the reported numbers,
   quoted TOGETHER with the ledger above (the subject errors are soft-field
   and invisible to F1 on their own).
1b. **DPO mechanism probe — DONE 2026-07-10.** 13 GDPR Art 37–39 paragraphs
   mentioning the DPO, in neither test set (`data/dpo_probe_sample.json`,
   `data/dpo_probe.postscreened.jsonl`), live-extracted under
   `derived_actors` (`data/dpo_probe_1run_derived_actors/run1.extracted.jsonl`,
   18 records, 0 errors). Result: every sentence with the DPO as grammatical
   subject kept the DPO (5/5, including the branch-order stress case
   art_38(5) 'shall be bound by secrecy' — an actor subject inside a passive
   construction); zero records snapped the DPO away; zero duty-bearer
   substitutions fired anywhere in the probe; passive-patient sentences
   (37(5) 'shall be designated') were correctly attributed to the
   controller/processor by the LLM itself. Caveats: single run, subject-level
   check only, no gold on other slots.

2. **How the `derived_actors` fixes are assessed (corrected 2026-07-10 —
   an earlier plan to live-re-extract test1 as a "held-out fix assessment"
   was WRONG twice over: test1 leaks dev/holdout paragraphs, and it contains
   no DPO/Annex-III-sub-point/refrain content, so it could not exercise the
   fixes at all).** The deterministic fixes are assessed *exhaustively* by
   the replay ledger (deterministic code has no stochasticity to sample; every
   changed record across all four artifact sets is enumerated above). The one
   stochastic behaviour — the LLM's DPO subject surviving live — gets a
   mechanism probe: a few GDPR Art 37–39 paragraphs in NEITHER test set,
   extracted live under `derived_actors`, subject-level check only (gate,
   small API cost).
3. **Gate (API cost):** full-corpus re-extraction with `derived_actors` when
   the KG build resumes. Reported numbers stay test2-vs-`actorkeep`.
4. Known residuals, measured not fixed: LLM wrong-actor attribution in
   multi-actor paragraphs (silent, soft-field — needs explicit eyes in the
   test2 review), out-of-source imports (span grounding 1.8% + the
   CONTEXT-method blind spot), recital definitional fragmentation,
   over-splits, U35-class truncations.
5. test1 is reportable NOWHERE — not for headline numbers, not for fix
   assessment. It is the diagnostic artifact that caught the `frozen` guard,
   and that is its entire role in the dissertation.
