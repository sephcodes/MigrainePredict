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

---

# Phase 1, Stage 3: Verification

## What it does

Checks the mapped statements **against each other** before anything is treated
as knowledge — per the interim report's Stage 3 design. Three checks:
**contradiction detection** (opposite deontic forces on the same subject and
predicate–object), **redundancy detection** (multiple statements expressing
the same constraint), and **cross-regulation conflict detection** (a curated
pattern table, flagship: AI Act Art 12 logging vs GDPR Art 5(1)(e) storage
limitation — the MigrainePredict scenario's central tension). Problematic
statements are flagged and held out of the auto-ingested graph; everything
else is marked verified.

**Input:** the canonical extraction run (run 4) of both content-mapped sets —
**51 statements** (33 deontic, 8 definitional, 10 applicability) loaded into a
Neo4j staging graph.

## Methodology

- **Staging graph + Cypher, exactly as the report specifies.** All candidates
  are loaded into Neo4j first and verified in place, with the three checks
  implemented as parameterised Cypher pattern-matches. Crucially,
  **verification persists as graph state**, not a side report: outcomes are
  typed relationships (`EXCEPTION_OF`, `CANDIDATE_CONTRADICTION`,
  `SPECIALISES`, `REDUNDANT_WITH`, `CONFLICTS_WITH`) plus a
  `verification_status` property — which is what the Phase 2 query layer
  consumes as traversable evidence.
- **Flag, never drop** (same discipline as extraction): no check destroys a
  statement. Tension/duplicate/conflict members are flagged for review;
  exception structures and specialisations are recorded as *informational*
  edges — positive findings about the regulation's architecture, not defects.
- **Ingest-with-provenance HITL:** LLM-suggested mappings don't block
  verification; they enter the graph carrying provenance labels
  (`:LLMSuggested` / `:HumanReviewed`), reviewable at any time — consistent
  with the mapping stage's uncalibrated-confidence finding.
- **The labelling standard: pair-local judgment, computed resolution.** The
  most consequential methodological decision. A human labels whether two
  statements clash *on the face of their own text*; whether the corpus
  elsewhere resolves the clash is a **graph traversal** (the resolution pass),
  not an annotation judgment. The natural alternative — label a contradiction
  only if "unresolvable" — was abandoned before use: it would require the
  annotator to hold the entire corpus in their head, and labels would silently
  rot as the corpus grows.
- **Expert-adjudicated gold with near-miss negatives:** the evaluation
  worksheet contains every pair the detectors surfaced *plus* all near-miss
  negatives (pairs with partial evidence that did not fire), so both precision
  and the plausible recall boundary are measurable. Human labels are preserved
  across regeneration (the mapping stage's status-based HITL pattern).

## Implementation highlights

- **Graph model:** `(:Statement:Candidate)` nodes; slot edges
  (`HAS_SUBJECT/PREDICATE/OBJECT/CONDITION`) to `(:Concept)` nodes carrying
  `mapping_status` + provenance method **on the edge** (slot-granular
  provenance); `(:Provision)` nodes with `SOURCED_FROM`/`REFERS_TO` edges from
  the extraction's references (statement-level references resolve to statement
  nodes); and a `BROADER` concept hierarchy parsed from the ontology index for
  subsumption-aware matching. Loader is idempotent; every load/verdict/flag
  writes a timestamped audit-log event.
- **Four scripts:** loader → verifier (the three Cypher checks + resolution
  pass, emitting a replayable verdict JSONL) → worksheet generator (HITL) →
  scorer (detector P/R vs human labels).

## Evaluation

The detector was evaluated against the human-labelled worksheet in two rounds
— and the round-1 failure is as reportable as the final result:

| Stage | Agreement | Tension P | Tension R | Exception P/R | Specialisation P/R | Duplicate FPs | Review queue (real) |
|---|---|---|---|---|---|---|---|
| Round 1 (naive detector) | 59.8% | 0.00 (0/20) | 0.00 (0/4) | 0.83 / 1.00 | 0.38 / 0.75 | 3 | 14 of 51 |
| Round 2 (refined, pre-review) | (100% — co-refined, discounted) | — | — | — | — | 0 | 0 of 51 |
| **Final (expert-reviewed)** | **95.5%** | **0.69 (9/13)** | **1.00 (9/9)** | **1.00 / 1.00** | **1.00 / 1.00** | **0** | **0 of 51** |

**What round 1 taught us (each failure has a named cause):**

- *IRI-overlap contradiction detection scored precision 0/20 AND recall 0/4
  simultaneously*, from one root cause: mapped IRIs are coarser than the legal
  norms. Twenty false alarms collapsed into two hubs where every pair reduced
  to controller × `Processing` × `PersonalData` — concepts so ubiquitous that
  overlap on them carries no information — while all four genuine tensions
  (processing norms vs Art 9(1)'s unconditional prohibition) were missed
  because their predicates had stayed literal text at mapping (no IRI to
  overlap).
- *Concept-subsumption specialisation* produced sibling-pair artifacts;
  *reference-linkage alone* over-classified exceptions (Art 30(5) references
  Art 9(1) as a condition, not a derogation); *object-IRI-equality duplicates*
  were 3/3 false alarms — IRI equality is evidence of vocabulary granularity,
  not redundancy.

**The round-2 fixes, all structure- or data-driven:** a discriminative-overlap
gate (a measured generic tier — concepts on ≥10% of deontic candidates —
whose members can't serve as overlap witnesses; killed all 20 false alarms);
an unconditional-prohibition rule (empty condition slot + object subsumption,
needing no predicate IRIs; recovered all 4 misses); structural specialisation
(chapeau → sub-point paragraph hierarchy; 4/4, zero artifacts); same-article
exception scoping; tightened duplicate criteria (0 firings). **The
cross-cutting lesson mirrors mapping: structure-driven rules (references,
paragraph hierarchy, deontic form) consistently beat similarity-driven rules
(concept overlap).**

**The resolution pass and the scaling argument.** All 13 detected tensions are
the Art 9(1) "hub" — legally real: every processing norm prima facie collides
with the corpus's one unconditional prohibition until an Art 9(2) basis covers
it. All 13 resolved *mechanically* through the Art 9(2) exception edges
already in the graph. **Real-statement review queue: 0 of 51** (down from 14
in round 1). This is the scaling answer for HITL verification: prima facie
tensions are absorbed by the regulation's own derogation structure, so the
human sees only what the graph cannot answer.

**The honesty layer.** The round-2 "100%" is reported only to be discounted —
the gold was co-refined with the detector, so it partly measured
self-agreement. The independent expert pass is what converts it into a
result: it confirmed the labelling standard *and* overruled four detector
firings (Art 33/34/35 and Art 32(1)(c) vs Art 9(1)), defining the residual
failure class: **operand-vs-mention** — `dpv:PersonalData` appears in those
objects only because the noun phrase mentions it ("the personal data
*breach*"); the obliged act doesn't operate on the data. The schema records
which concepts appear in a slot but not their *role* — a concrete granularity
limit of slot-level ontology grounding, deliberately left unfixed (four rows
don't justify new machinery).

**Update since the retrospective — the conflict demo is now real data.** The
flagship cross-regulation conflict was initially demonstrated on an injected
synthetic pair (neither provision was in the 50-record subset). Since then,
the real pair — GDPR Art 5(1)(e) + AI Act Art 12(1) — was run through the
full extract → map → verify pipeline as a mini-extraction: the detector fires
`CONFLICTS_WITH` on the **real statements** (logging obligation vs storage
limitation), auto-detects the Art 5(1)(e) archiving dispensation as an
`EXCEPTION_OF`, and the conflict pair passed human legal review. The current
scorer output (the reproducible state of the graph today):

```
rows scored: 105  (skipped unlabelled: 0)
overall agreement: 100/105 = 95.2%

== contradiction  (86 pairs, agreement 81/86)
   candidate_contradiction   P = 10/14 = 0.71   R = 10/11 = 0.91
   exception_structure       P = 6/6 = 1.00   R = 6/6 = 1.00
   confusion: detector=candidate_contradiction -> human=none  x4
   confusion: detector=none -> human=candidate_contradiction  x1

== redundancy  (18 pairs, agreement 18/18)
   specialisation            P = 4/4 = 1.00   R = 4/4 = 1.00

== cross_regulation  (1 pairs, agreement 1/1)
   conflict                  P = 1/1 = 1.00   R = 1/1 = 1.00
```

Reconciling with the arc table above: the table's final row is the
**stage-close** state (89 pairs, before the real conflict pair); this output
is the **current** state after the conflict-pair rows were added and reviewed.
Tension precision ticked up (0.69 → 0.71, one more true positive among the new
pairs); recall moved off 1.00 (0.91) because the human review of the new rows
confirmed one detector false negative — the Art 17(1) erasure obligation vs
the Art 5(1)(e) archiving dispensation, a pair-local tension the detector did
not surface (the `none → candidate_contradiction ×1` confusion line). The four
precision misses remain the known operand-vs-mention class. Final graph:
**54 statements, all verified, zero synthetic content**.

## Findings worth presenting

- **Structure beats similarity on legal text** — the graph's explicit
  reference and hierarchy edges are the signal; concept-overlap inherits every
  granularity defect of the vocabulary.
- **Art 9(1) as a "legally real hub":** tension detection + the resolution
  pass recover the regulation's own architecture (general prohibition +
  enumerated derogations) as graph structure.
- **An empty review queue is a result, not an absence** — evidence that
  human attention scales with genuine unresolved tension, not corpus size.
- **Co-refined gold is not gold until independently adjudicated** — the
  round-2 100% → expert-reviewed 95.5% arc is a methodological finding in
  itself.
- **One planned tool superseded, not skipped:** OOPS! (ontology pitfall
  scanner) was in the plan for redundancy checking, but it audits ontology
  *schemas* — since the final pipeline reuses DPV/AIRO/VAIR wholesale and
  authors no classes, OOPS! would audit the ADAPT Centre's ontologies, not
  this project's contribution. The typed-edge redundancy check does what
  Stage 3 actually required.
- **The HITL pattern is now uniform across the pipeline:** what reaches a
  human is decided by deterministic structure (unresolved tension, duplicate,
  conflict) or explicit status — never by a model's self-reported confidence.
  The safety mechanism never assumes the model knows when it is wrong.

---

# Phase 1, Stage 4: Integration

## What it does — and the decision that made it small

The original plan treated integration as its own step: promote verified
candidates from a staging area into the knowledge graph. The settled decision
collapses that: **the staging graph *is* the knowledge graph.** Because
verification outcomes already persist as graph state (typed edges +
`verification_status`) and provenance has been carried on every slot edge
since mapping, a second ingestion would only copy data and create a
synchronisation liability. Integration therefore reduces to two things:
**disposition** (which statements the query layer may use) and
**auditability** (a replayable record of how every statement got its status).

## Implementation

- **The `:Verified` label is the integration contract.** Every non-flagged
  candidate gets `:Verified`; flagged statements lose it. Phase 2 queries
  filter on this one label — flagged statements are held out of query results,
  not deleted (the flag-never-drop discipline carried through to the end).
- **Human dispositions live in a tracked repo file, never in ad-hoc database
  edits.** The reviewed verification worksheet *is* the sign-off: the verifier
  consumes it directly — a human label agreeing with the detector confirms the
  flag's resolution; a `none` label overrules the detector and deletes its
  edge; an unlabelled statement stays flagged. (An earlier ad-hoc terminal
  `SET` to restore the reviewed conflict pair was rejected as unauditable and
  replaced by this mechanism — the same lesson as ground-don't-assert: a human
  decision must be a tracked artifact a script consumes, so the graph state is
  always derivable from the repo.)
- **Audit log (the report's NFR1):** every wipe, statement load, verification
  verdict, human disposition, and detector overrule appends one timestamped
  JSONL event. The current log for the full build: **265 events — 1 wipe, 54
  statements loaded, 208 verification verdicts, 2 statements verified after
  human review** — and every event maps to a repo code path.

## Evaluation

Integration has no accuracy metric of its own — its deliverables are
properties, each verified:

- **Reproducibility (proven):** a clean rebuild — wipe → load the three
  statement sets (DEV, HOLDOUT, conflict pair) → verify with reviewed
  dispositions applied — was run end-to-end and converges to the identical
  reviewed graph. The knowledge graph is a deterministic function of the
  tracked repo files.
- **Provenance completeness:** every slot edge carries its mapping status
  (auto / LLM-suggested / human), every statement carries
  `:LLMSuggested`/`:HumanReviewed` labels where applicable, and every
  statement links to its source provision with the original paragraph text —
  so any answer Phase 2 produces can be traced to regulation text and to who
  or what vouched for each mapping.
- **Final graph state: 54 statements, 54 `:Verified`, 0 flagged, 0
  synthetic**, with the real cross-regulation `CONFLICTS_WITH` edge and the
  `EXCEPTION_OF` derogation structure in place — the Phase 2-ready knowledge
  graph.
