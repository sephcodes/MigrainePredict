# Corpus extraction acceptance testing — running summary

Companion to `mapping_stage_summary.md` / `verification_stage_summary.md` /
`graphrag_stage_summary.md`. This defines every name used in the corpus-scale
extraction evaluation (2026-07-10): the two test sets, the three pipeline
states, and every file. The layout mirrors the dev/holdout convention:
`data/<set>_gold_set.jsonl`, `data/<set>.postscreened.jsonl`,
`data/<set>_<N>run_<tag>/runN.extracted.jsonl`, where the tag names the
pipeline state that produced the run.

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
| `data/test2_gold_set.jsonl` | authored gold U01–U46, under Yoseph's review |
| `data/test2_1run_actorkeep/run1.extracted.jsonl` | `actorkeep` live extraction — the reported run |
| `data/test2_1run_derived_actors_replay/run1.extracted.jsonl` | `derived_actors` post-passes replayed over the saved `actorkeep` run (no LLM) |

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

## Status and open items

1. Yoseph's test2 gold review — in progress. Its grade vs
   `data/test2_1run_actorkeep/run1.extracted.jsonl` = the reported numbers.
   Open gold decision: U12 modality (permission-restricted vs
   prohibition-with-exception).
2. **Gate (API cost):** live `derived_actors` re-extraction of test1 = the
   held-out fix assessment — test1 motivated none of the `derived_actors`
   fixes, so it validly measures them (frozen/actorkeep/derived_actors
   comparison on the same gold). Also the only way to prove the DPO fix live.
3. **Gate (API cost):** full-corpus re-extraction with `derived_actors` when
   the KG build resumes. Reported numbers stay test2-vs-`actorkeep`;
   `derived_actors` is the deliverable pipeline, its evidence = item 2.
4. Known residuals, measured not fixed: LLM wrong-actor attribution in
   multi-actor paragraphs (silent, soft-field — needs explicit eyes in the
   test2 review), out-of-source imports (span grounding 1.8% + the
   CONTEXT-method blind spot), recital definitional fragmentation,
   over-splits, U35-class truncations.
