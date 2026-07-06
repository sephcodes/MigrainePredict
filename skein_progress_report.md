# Skein Progress Report — MigrainePredict Compliance Knowledge Graph

Progress update covering the state of the project: each stage of knowledge-graph
construction (extraction, mapping, verification, integration) with methodology,
implementation, and evaluation results, plus the initial test of KG querying.

---

# Phase 1, Stage 1: Extraction

## What it does

Turns the raw regulatory text (GDPR + EU AI Act, retrieved as XHTML via ELI
content negotiation) into typed, machine-readable statements. Every paragraph
becomes zero or more of three statement classes — **deontic** (obligations,
prohibitions, permissions, dispensations), **definitional** ("'personal data'
means…"), and **applicability** (territorial/material scope) — each validated
against a Pydantic schema before anything enters the graph.

## Methodology

- **Anchored on Galli et al.'s six-element deontic framework** (subject,
  modality, predicate, object, condition, references), adapted into three
  Pydantic models. Every extracted value carries a provenance method
  (STATED = grounded in the paragraph text vs CONTEXT = inferred), plus the
  source paragraph text itself for end-to-end traceability.
- **Two-stage LLM extraction per paragraph** (Gemini 2.5 Flash, temperature 0):
  Stage 1 classifies what statement types a paragraph contains; Stage 2 makes
  one typed structured-output call per candidate. Chunking uses a
  structure-aware pipeline (Markdown parsing → small-chunk merging → sentence
  splitting) after semantic chunking was tested and rejected — adjacent legal
  sentences are deliberately thematically similar, so similarity-based
  splitting fails on regulation.
- **The core engineering principle: deterministic post-processing over prompt
  engineering.** Every prompt edit we tried destabilised unrelated fields;
  every deterministic guard held. So the LLM does the language understanding,
  and a suite of **11 deterministic post-passes plus an enumeration gate**
  enforces correctness — keyed on structural facts (IRI paths, parent text,
  modality), not prompt instructions.
- **Human-in-the-loop by design, with a strict drop-vs-flag discipline:** a
  guard only destroys/rewrites a value when the correction is provably safe
  (e.g. removing a redundant exception-split mirror); anything uncertain is
  flagged `needs_review` and routed to human review instead. Low-confidence
  extractions (< 0.7) are flagged automatically. Silent data loss is the
  failure mode we designed against.

## Implementation highlights

- **Enumeration gate:** legal sub-points like Art 17(1)(a)–(f) are *conditions*
  of one obligation, not independent statements — the gate detects
  condition-introducing parent text (whitelist of legal formulae) and re-routes
  those sub-points to the deontic extractor, while content lists like the
  Art 5(1) principles correctly stay independent. Covered by a standing
  regression test.
- **Post-pass suite** (all deterministic, all regression-tested where
  non-trivial): predicate normalisation to clean verb lemmas (shared module
  used identically by the extractor and the evaluation harness, so the graph
  holds clean predicates and grading stays consistent); Annex III area
  grounding; subject canonicalisation to a role vocabulary with
  regulation-default duty-bearers; recital-scope guards; exception-split
  merging; stable statement IDs; intra-paragraph cross-references (a permission
  is linked to the prohibition it derogates from); and four flag-only quality
  guards (unattributed references, truncated spans, deontic-operator predicates
  like "prohibit processing" that a reasoner would misread as a double
  negative, redundant negation on prohibitions).

## Evaluation

Hand-authored gold sets: **DEV = 23 records, HOLDOUT = 27 records** (holdout
authored blind after the pipeline stabilised). Scored over **5 independent
runs** because temperature 0 is not deterministic — all numbers are 5-run
means. The precision/recall mapping (lenient-recall: a matched-but-imperfect
statement is charged to precision, not recall) is written up and defended in
`evaluation_methodology.md`.

### HARD vs soft criteria

Every gold-vs-run field comparison is classed as **HARD** or **soft**:

- **HARD fields** are the objective, machine-checkable ones where there is a
  single right answer: statement class, deontic modality, applicability scope
  type and polarity, the defined term, the applicability target value
  (`applies_to`), the definition text, NA status, and `needs_review` when the
  gold asserts it. A HARD mismatch means the extraction got a fact of the
  statement wrong.
- **Soft fields** are the interpretive ones where correct answers can be worded
  differently: predicate, object, condition, subject, beneficiary, severity,
  and cross-references. A soft flag means the wording differs from gold but the
  proposition is intact.

Mapping to the confusion matrix: a matched record with **zero HARD failures**
counts as a true positive regardless of soft flags (soft flags are reported
separately as a quality layer, not an error count). A matched record with **≥1
HARD failure** is charged to **precision only** — the proposition was detected
(so not a recall miss) but rendered unfaithfully. Unmatched gold records are
recall misses; unmatched run records are precision extras.

### Headline results

| Metric (COMBINED-50, 5-run mean) | Score |
|----------------------------------|-------|
| Precision                        | 0.881 |
| Recall                           | 0.984 |
| F1                               | 0.929 |
| F2 (recall-weighted; a missed obligation is a silent compliance gap, an extra is prunable) | **0.961** |

Per-category breakdown (COMBINED-50, mean over 5 runs, [min, max]):

```
category       |          P           |          R           |          F1          |          F2
----------------------------------------------------------------------------------------------------------
Deontic        | 0.874 [0.853, 0.879] | 1.000 [1.000, 1.000] | 0.933 [0.921, 0.935] | 0.972 [0.967, 0.973]
Definitional   | 0.825 [0.625, 0.875] | 0.892 [0.833, 1.000] | 0.855 [0.714, 0.933] | 0.876 [0.781, 0.972]
Applicability  | 0.907 [0.818, 1.000] | 1.000 [1.000, 1.000] | 0.949 [0.900, 1.000] | 0.979 [0.957, 1.000]
Not_Applicable | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000]
OVERALL        | 0.881 [0.855, 0.907] | 0.984 [0.979, 1.000] | 0.929 [0.913, 0.942] | 0.961 [0.951, 0.976]
----------------------------------------------------------------------------------------------------------
mean counts  M_clean(TP)=48.6  M_hard=0.6  MISS(FN)=0.8  EXTRA=6.0
soft quality layer  mean soft flags=83.8  over 49.2 matched records  (1.70/record)  [soft = correct for P/R/F1/F2]
```

Reading the counts: precision drag comes almost entirely from **extras**
(statements with no gold counterpart, dominated by the known Art 32 sub-point
class below), not from unfaithful matches — matched-but-HARD-failed records
average 0.6 per 50. Recall is near-ceiling: on average 0.8 gold records missed
per run.

The best run (run 4: P 0.891 / R 1.000 / F1 0.942 / F2 0.976) was selected as
the canonical run that feeds the downstream stages.

### Supplementary evaluations

**Schema validity** — 100% first-attempt Pydantic validity across all 256 typed
extractions in the saved runs; 100% re-validation.

**Hallucination check (span grounding)** — every STATED value tested for
content words absent from the source paragraph (and its parent chain). Result:
**2 flags out of 529 STATED content elements across 5 runs**; zero invented
content — both flags are genuine minor imports from adjacent provisions:

```
=== dev: 1 flagged / 150 STATED content elements over 5 run(s) ===
  1/5 [condition] gdpr:art_9/par_2/pt_h  not-in-source=['data', 'responsibility', 'obligation', 'secrecy', 'rules', 'established', 'national', 'competent', 'bodies', 'another', 'person', 'also', 'obligation', 'secrecy', 'rules', 'established', 'national', 'competent', 'bodies']
        'processing is necessary for the purposes of preventive or occupational medicine, for th...'

=== holdout: 1 flagged / 379 STATED content elements over 5 run(s) ===
  2/5 [object] gdpr:art_17/par_3  not-in-source=['provisions']
        'the provisions of paragraphs 1 and 2'
```

The dev flag imports Art 9(3)'s professional-secrecy wording into an Art 9(2)(h)
condition (real, once in 5 runs); the holdout flag is the single word
"provisions" added to a paragraph reference.

**Cross-run stability** — the 5 runs aligned into statement slots, agreement
measured per slot and per field:

```
SET: dev   (5 runs, 25 aligned slots)
  present in all 5 runs:                     23/25 = 92.0%
  HARD-agreement (objective fields agree):   24/25 = 96.0%
  fully stable (present + all fields agree): 18/25 = 72.0%

SET: holdout   (5 runs, 33 aligned slots)
  present in all 5 runs:                     30/33 = 90.9%
  HARD-agreement (objective fields agree):   32/33 = 97.0%
  fully stable (present + all fields agree):  9/33 = 27.3%
```

The propositions are substantively stable (96–97% HARD-agreement); the gap down
to "fully stable" is surface wording of the interpretive fields and
STATED-vs-CONTEXT method wobble, not disagreement about what the law says.

**Cycle-consistency** — deontic statements serialized back to plain sentences
with a deterministic template and re-extracted 5×; round-trip drift is at or
below the cross-run baseline on essentially all fields → the representation is
effectively lossless.

## Honest residuals

- One flaky error class remains: **definition-text truncation** on 3 records
  (the model intermittently includes/excludes the "'X' means" prefix) — it is
  the main driver of the flaky Definitional cell above (P min 0.625).
- The **Art 32 measure sub-points** are extracted as independent obligations
  with no gold counterpart. They account for most of the EXTRA count.
- Run-to-run variance at temperature 0 is real and is why everything is judged
  across 5 runs, never one.

---

# Phase 1, Stage 2: Mapping

## What it does

Grounds every element of the extracted statements against the ontology stack —
**DPV** (plus its eu-gdpr, sector-health, and justifications extensions) for
GDPR, **AIRO/VAIR** for the AI Act — so statements stop being free text and
become graph-ready. This realises the project's first principle: *constrained
generation over ontology discovery* — the system may only **map** to a fixed,
routed vocabulary (2,233 concepts indexed from the ontology TTLs); it can never
invent a concept. Schema drift (the weakness identified in Turaga's approach)
never occurred.

The six statement slots are grounded by three passes of increasing difficulty:

1. **Subject** — a deterministic alias lexicon mapping actor phrases to
   canonical role IRIs (`the controller` → `dpv:DataController`, `the provider`
   → `airo:AIProvider`), with a regulation-side check. No lexicon hit, or a
   wrong-regulation hit, is flagged for review — never force-fit.
2. **Modality** — a 1:1 deterministic map from the four deontic modalities to
   relation IRIs (`OBLIGATION` → `mp:hasObligation`, …), following Echenim's
   four-relation scheme.
3. **Predicate / object / condition** — the open-class content slots, and the
   substance of this stage: a five-component pipeline described below.

## Methodology

- **Deterministic-first, LLM only for the residual tail.** A deterministic
  matcher auto-disposes the bulk of values (exact and synonym/alias hits, plus
  IDF-weighted lexical matching that discounts generic tokens and prefers the
  most specific concept); embedding similarity (bge-small) is computed but
  treated as a **non-authoritative hint only** — over a closed, homogeneous
  legal vocabulary the nearest neighbour always scores ~0.65–0.80 even when the
  right answer is "no concept", so it never decides anything. Only the
  genuinely undecided rows go to the Tier-3 LLM adjudicator (Gemini 2.5 Flash,
  same structured-output setup as the extractor).
- **Slot routing as the drift guard.** A routing table declares, per slot ×
  regulation, which vocabulary schemes are valid targets — so the matcher and
  adjudicator can only ever choose from routed concepts. Routing was assigned
  once, deductively, by content-kind (every DPV scheme placed or explicitly
  excluded), which means an unmapped value afterwards is a **genuine vocabulary
  gap**, not a forgotten scheme.
- **De-duplication makes it tractable.** The adjudication worksheet has one row
  per distinct (slot, regulation, value, article-root) with an occurrence
  count — a decision is made once and applied to every occurrence. Distinct
  values saturate rather than growing with corpus size, which is what makes
  full-corpus scale-up feasible.
- **Status-based human-in-the-loop, not confidence-thresholded.** Human
  decisions (`manually_*`) and LLM proposals (`llm_suggested_*`) are distinct,
  first-class statuses preserved verbatim across matcher re-runs. This is a
  deliberate, evidence-driven correction to the interim report's plan (see the
  calibration finding below).
- **Flag, don't force-fit.** A real concept with no vocabulary home becomes a
  `flag` — a first-class output and a research finding — rather than being
  silently attached to a near-miss.

## Implementation highlights

- **Pipeline:** vocabulary indexer (ontology TTLs → flat concept index) → slot
  routing table → deterministic matcher (writes the de-duplicated adjudication
  worksheet) → LLM adjudicator (propose-only: writes `llm_suggested_*` with
  confidence and rationale, validated against the vocabulary so no invented
  IRIs; out-of-vocabulary proposals are escalated, not trusted) → scorer
  (adjudicator vs human gold).
- **Adjudication worksheet (final, human-reviewed):** 126 distinct rows across
  the three content slots — predicate: 18 auto-mapped / 18 literal / 6
  no-target; object: 34 auto-mapped / 6 LLM-suggested maps / 5 literal;
  condition: 9 manually mapped / 14 LLM-suggested maps / 14 literal / 2
  flagged genuine gaps.
- **Apply-back (`map_content.py`):** the adjudicated worksheet is merged back
  onto the extraction records, chained on the subject- and modality-mapped
  output, joining by the matcher's exact (slot, regulation, value,
  article-root) keys. The join was verified with a dry run before writing:
  **0 misses across all 276 records** (predicate 92 mapped / 69 literal / 22
  no-target; object 144 mapped + 18 LLM-suggested + 8 manual; condition ~80
  mapped / 48 literal / 10 flag). Each element carries both its **IRI list and
  its `mapping_status`**, so auto vs LLM-proposed vs human provenance survives
  into the graph — downstream stages can enforce "human-confirm before graph
  entry" as a one-line filter on status rather than trusting an IRI blindly.
  Genuine-gap flags (the quantitative thresholds below) also ride through as
  status + empty IRI, so the finding survives into the graph rather than
  vanishing.
- **End-to-end result:** a fully grounded statement. Example (Art 6(1)(a)):
  subject → `dpv:DataController`, modality → `mp:hasPermission`, predicate →
  `dpv:Processing`, object → `dpv:PersonalData`, condition →
  `dpv:ConsentGiven` (status `llm_suggested_mapped`), source article
  `gdpr:art_6/par_1/pt_a`.

## Evaluation

The LLM adjudicator was scored against the human-adjudicated gold worksheet —
an **inter-annotator-style agreement** metric (agreement with a single expert,
reported as such), scored only on rows the LLM actually decided, so auto rows
cannot inflate agreement. The iteration arc:

| Stage                                    | Disposition agreement | IRI exact | IRI overlap | Bottleneck found |
|------------------------------------------|-----------------------|-----------|-------------|------------------|
| Initial (candidate-constrained)          | ~68%                  | 17-24%    | 67-80%      | retrieval ceiling: ~39% of "errors" were the right concept never being offered |
| + full routed vocabulary, slot merge     | ~72–81%               | 5–10%     | ~72%        | over-inclusion of generics and article-code legal bases |
| + deterministic exclusions, polarity fix | **~78%**              | ~21%      | **~89.5%**  | residue = defensible supersets + judgment calls |

The final row is the last `score_adjudication.py` run verbatim:

```
adjudicated pairs scored: 27   (gold decisions the adjudicator didn't touch, excluded: 93; gold rows missing from prediction: 0)
escalated by adjudicator: 0  (0%)

disposition agreement (non-escalated): 21/27 = 77.8%
  IRI exact-match  (both mapped): 4/19 = 21.1%
  IRI any-overlap  (both mapped): 17/19 = 89.5%

confusion (gold -> pred):
  literal   -> literal      2
  literal   -> mapped       6   <-- mismatch
  mapped    -> mapped       19

agreement by confidence bucket (for threshold choice):
  0.7-0.9: 1/2 = 50%
  0.9-1.0: 20/25 = 80%
```

Reading the final confusion matrix: every remaining mismatch runs in the
`literal → mapped` direction (the adjudicator proposing a concept where the
human said leave it as text) — zero missed concepts in the other direction.
That is exactly the assistant profile: the human confirm pass prunes extras;
it never has to rescue a missed mapping.

**The calibration evidence, as measured:** across all scored iterations the
0.9–1.0 confidence bucket sat at 68–81% agreement (68%, 75%, 81%, 80% by run),
and the <0.7 escalation valve fired **0 times in every run** — the model never
once said "I'm unsure." This is the direct measurement behind "no confidence
band can be auto-accepted."

Decomposing the final gap was itself a finding: most non-exact rows are
**defensible supersets** (all the gold concepts plus one arguable extra), a few
are **judgment calls** where the model is arguably right, and a few are
**over-inclusion on long multi-purpose clauses** — exactly where a human
confirm adds value. None is a systematic error a further code change cleanly
addresses.

**The key negative finding — adjudicator confidence is uncalibrated.** Errors
persisted at confidence 0.9–1.0 through every iteration, so **no confidence
band can be auto-accepted**. The interim report specified
confidence-thresholded routing to expert review; the data falsified that for
this stage. The workable mechanism is **status-based**: deterministic
disposition decides what is auto-accepted, and every LLM suggestion is
human-confirmed before entering the graph regardless of its confidence. The
adjudicator is therefore an **assistant** (a fast confirm pass — prune an
extra, not research from scratch; the 89.5% concept overlap is what makes that
genuinely time-saving), **not an autonomous mapper**. This is a defensible
refinement of the report's HITL requirement, and a citable result about where
LLM-assisted ontology population needs a human.

## Instructive failures (tried, measured, reverted)

Each was a plausible idea the data falsified — the same cross-cutting lesson as
extraction: deterministic, empirically-justified code beats prompt guidance.

- **Prompt-level rules against generic ride-alongs** failed — the model mapped
  `Law` at confidence 1.0 despite instructions. Replaced by deterministic
  exclusions, each justified by measuring that the gold uses the excluded
  concept zero times (article-code legal bases were the largest precision leak;
  excluding them roughly quadrupled IRI exact-match).
- **Confidence-thresholded escalation** — the intended safety valve — does not
  work on an uncalibrated judge (the finding above).
- **Wide "route everything" routing** and a **head-noun gate** for objects both
  made results worse and were reverted to simple, scoped rules.
- A mid-project **per-slot coverage metric** was abandoned once it started
  driving design choices — optimising it was corrupting the graph. Mapping
  correctness is the objective.

## Findings worth presenting

- **DPV cannot represent GDPR's quantitative thresholds** — the 250-employee
  Art 30(5) exemption and the 72-hour Art 33 breach deadline surfaced as clean
  `flag`s (verifiable as genuine vocabulary gaps because routing is complete by
  construction). Concrete evidence for the "extend existing ontologies"
  recommendation and a candidate contribution.
- **Loading the DPV extensions matters:** eu-gdpr, sector-health (the medical
  purposes — directly relevant to MigrainePredict), and justifications closed
  real gaps the human kept finding by hand; several apparent gaps became clean
  maps.
- **Tag pass (validated improvement, applies from corpus scale-up):** quoted
  defined-term markers in the regulation's own text (e.g. *('storage
  limitation')*) are harvested deterministically and injected as mapping
  candidates. Validated end-to-end with zero human edits on a fresh build: it
  rescues concept mappings the lexical matcher misses (notably the
  storage-limitation condition central to the cross-regulation conflict demo),
  with pollution filters shown to hold. Kept out of the frozen evaluation
  numbers above; the measured before/after is reported as its own result.
