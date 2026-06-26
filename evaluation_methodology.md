# Extraction evaluation methodology

Scope: how the Phase-1 extraction pipeline (`extract_min.py`) is scored against
the hand-authored gold reference. This document fixes the mapping from the
grading harness's HARD/soft outcome to a confusion matrix and the resulting
Precision / Recall / F1 / F2, so the scoring is a defended methodological choice
rather than an implicit one.

- Grading (field-by-field diff, HARD/soft tiers): `compare_to_gold.py`.
- Scoring (confusion matrix, P/R/F1/F2): `score_extraction.py` (reuses the
  grading alignment verbatim).

## 1. Two field tiers (recap)

Each gold field is classed **OBJECTIVE** (single correct answer; mismatch = HARD
fail — wrong class, modality flip, truncated/wrong `applies_to.value` or
`definition.value`, dropped reference, wrong `needs_review` where the gold
asserts a deterministic gate outcome) or **INTERPRETIVE** (reasonable wording or
annotators may differ; mismatch = SOFT flag — predicate wording, subject voice,
condition/object phrasing, beneficiary, severity, healthcare tag, and
references, which are graded soft by `REFERENCES_HARD = False`). The exact field
lists live in `compare_to_gold.py` (`OBJECTIVE_STMT` / `INTERPRETIVE_STMT`).

## 2. Record buckets after alignment

Records are aligned by `(paragraph_iri, discriminator)` with content best-match
inside each group (so multiple statements per key align correctly). Each gold
and run record then falls into exactly one bucket:

| Bucket    | Definition                                              |
|-----------|---------------------------------------------------------|
| `M_clean` | gold record matched by a run record with **0** HARD fails |
| `M_hard`  | gold record matched by a run record with **≥1** HARD fail |
| `MISS`    | gold record with **no** matching run record             |
| `EXTRA`   | run record with **no** matching gold record             |

Every run record is exactly one of `M_clean`, `M_hard`, `EXTRA`; every gold
record is exactly one of `M_clean`, `M_hard`, `MISS`.

## 3. Confusion-matrix mapping (the methodological choice)

We adopt a **lenient-recall** scheme:

```
TP = M_clean
FP = M_hard + EXTRA          # extracted, but wrong field or unwarranted record
FN = MISS                    # a gold proposition that never surfaced
P  = TP / (TP + FP) = M_clean / (M_clean + M_hard + EXTRA)
R  = TP / (TP + FN) = M_clean / (M_clean + MISS)
F1 = 2·P·R / (P + R)
F2 = 5·P·R / (4·P + R)       # F_beta, beta = 2 (recall weighted 2× precision)
```

**Why a HARD-failed match (`M_hard`) is charged to precision only.** It is both
(i) a wrong emission — correctly an FP — and (ii) a gold proposition that was, in
fact, *detected* at the right key (the extractor found a statement of the right
class in the right paragraph; it got an objective field wrong). We attribute the
error to precision and decline to also count it as a recall miss. The stricter
alternative adds `M_hard` to FN, penalising both P and R:

```
strict:  FN = MISS + M_hard ;  R = M_clean / (M_clean + M_hard + MISS)
```

We report `M_hard` explicitly in every block, so a reviewer can recompute the
strict figure. In practice `M_hard` is small (≈1 record/run), so the two schemes
differ marginally; the lenient choice keeps recall a measure of *detection*
(did the proposition surface at all) and precision a measure of *fidelity +
restraint* (was every emission correct and warranted).

**Why F2.** Compliance extraction is recall-sensitive: a missed obligation is a
silent compliance gap downstream, costlier than an over-extraction a human
reviewer can prune. F2 weights recall 2× precision; we report F1 alongside it.

## 4. Soft flags are reported separately, not scored

SOFT mismatches do **not** enter the confusion matrix: a soft mismatch does not
break the proposition, so the record counts as `M_clean` for P/R/F1/F2. Soft
flags are reported as a separate **quality layer** (mean soft flags per matched
record). This is a deliberate choice — "we ignored N soft flags" is defended,
not silent: the interpretive fields have no single correct answer, the gold
value is an adjudicated convention, and counting convention drift as extraction
error would conflate "wrong" with "phrased differently." The quality layer keeps
that drift visible without letting it depress the headline scores.

## 5. Per-category breakdown

Because the gold spans three substantive statement classes that fail
differently, every block reports P/R/F1/F2 per class — **Deontic**,
**Definitional**, **Applicability** — and an **Overall**. `Not_Applicable` is
reported for completeness but is not a substantive extraction target. A category
with no records in a set is shown as "— (no records)", not 0.000.

## 6. Aggregation

The extractor (Gemini 2.5 Flash, temperature 0) has real run-to-run variance, so
a single run is not reported. Each metric is the **mean over 5 runs**, annotated
`[min, max]`. Results are reported per set — **DEV (23 records, G01–G23)** and
**HOLDOUT (27 records, H01–H29)** — and **COMBINED (50)**, where COMBINED pools
the bucket counts run-index by run-index before computing metrics.

## 7. Known characteristics surfaced by this metric (not faults in the metric)

- **`EXTRA` dominates the precision drag**, not `M_hard`. The stable holdout
  extras are the 4 Art 32(1) measure sub-points (`art_32/par_1/pt_a..d`), emitted
  as DEONTIC obligations with no gold counterpart — an open modelling decision
  (add gold / accept / fold into the chapeau object), not a wrong field.
- **Definitional precision/recall is the flakiest cell**, driven by the
  definition-text canonicalization class (G10 'personal data', H01 controller,
  H02 consent — intermittent inclusion of the `"X" means` prefix in
  `definition.value`).

## 8. Provenance of the reported runs (no fresh LLM run required)

The reported figures come from the two latest 5-run extractions —
**dev = `data/dev_5run_prednorm`** (extracted 2026-06-23 19:15) and
**holdout = `data/holdout_5run_newgold`** (extracted 2026-06-25 15:20) — brought
up to date with the current pipeline *deterministically*, without new LLM calls.

The stage-1/stage-2 LLM output is frozen in each saved run; only deterministic
post-passes and the stage-1→2 enumeration gate changed afterwards. We verified
this is safe to replay rather than re-extract:

- **Enumeration gate** (the only change that can re-route to a different LLM
  extractor, so the only one needing a live call): both runs postdate the gate
  commit, so it is already baked into the frozen output. The gate's firing
  points (`gdpr:art_9/par_2/pt_h` in dev; `art_6/par_1/pt_a`, `art_9/par_2/pt_j`,
  `art_9/par_2/pt_a` in holdout) were each already classified DEONTIC in the run,
  so the re-route is a no-op there. No record is changed by the gate.
- **Post-passes** were replayed onto the saved runs with `replay_postpass.py`
  (it re-runs the exact post-pass sequence from `_process_paragraph`). Validated
  diffs:
  - dev: only `art_5/par_1/pt_b` changed — predicate `"further processed" →
    "further process"` (predicate-norm). Predicate is a **soft** field → **no
    change to P/R/F1/F2**; the overwrite only refreshes the stored verb form.
  - holdout: only `aiact:anx_III/par_1` (H13) changed — `applies_to`
    `{"AI systems", CONTEXT}` → `{"Biometrics", STATED}` (the Annex-III area
    fix). `applies_to.value` is **objective**, so this corrects a HARD fail in
    3/5 runs (holdout Applicability precision 0.70 → 1.00).

Both run directories were overwritten in place with the validated replayed
output, so they now equal what the current pipeline would emit for the frozen
LLM sample. (Run outputs are gitignored; the durable artifacts are the gold
sets, the scoring/replay scripts, and this note.)

## 9. Reproduce

```
# self-test: must print P=R=F1=F2=1.000, 0 soft
python score_extraction.py --selftest data/gold_set.jsonl

# bring a saved run up to date with current post-passes (no LLM); inspect diff,
# then re-run with --in-place to overwrite
python replay_postpass.py --input <postscreened...> --run <run.extracted.jsonl>

# score the 5-run sets, per-set + COMBINED-50
python score_extraction.py \
    --set dev:data/gold_set.jsonl:data/dev_5run_prednorm \
    --set holdout:data/holdout_gold_set.jsonl:data/holdout_5run_newgold
```
