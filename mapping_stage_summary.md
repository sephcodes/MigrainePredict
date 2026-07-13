# Content-Mapping Stage — Engineering Retrospective

*Phase 1, "Mapping" sub-stage of the MigrainePredict compliance-KG pipeline (Extraction → **Mapping** → Verification → Integration).*

This document summarises the strategies, dead ends, techniques, fixes, and decisions from the content-mapping work, and ties them back to the interim report where relevant. It is written to feed the dissertation's **methodology**, **implementation**, and **limitations/evaluation** sections.

---

## 1. What this stage is, and where it sits

Extraction (Stage 1) produces deontic statements using Galli's six-element decomposition (modality, subject, predicate, object, source article, condition), validated by Pydantic. The **subject** and **modality** slots were already grounded to fixed IRIs and merged back onto the extraction records. This stage handles the remaining, open-class slots — **predicate, object, condition** — aligning their free-text values to concepts in the ADAPT stack (**DPV** for GDPR, **AIRO/VAIR** for the AI Act).

This is the concrete realisation of the interim report's first principle — *"constrained generation is prioritised over ontology discovery … the model must map and classify rather than invent"* — and of FR2 (*ground all extracted concepts against the ADAPT stack*). Every mapping is drawn from a fixed, routed vocabulary; nothing is invented.

---

## 2. The pipeline we built

Five components, deterministic-first with an LLM only for the residual tail:

- **`build_vocab_index.py`** — parses the ontology TTLs into `terms.json` (a flat `{iri: {label, scheme, parents, types}}` index). Loads DPV core + PD + AIRO + VAIR, and (added mid-project) the DPV extensions **eu-gdpr**, **sector-health**, **justifications**.
- **`slot_routing.json`** — declares, per (slot × regulation), which vocabulary *schemes* are valid targets. This is the schema-drift guard: the matcher and adjudicator can only ever choose from routed concepts.
- **`build_content_candidates.py`** — the deterministic matcher. For each distinct slot value it does exact / synonym / alias / IDF-weighted lexical matching (with subsumption to drop subset labels) plus non-authoritative embedding hints (BAAI/bge-small), then assigns a **disposition**: `mapped` (auto), `review` (needs a decision), `literal` (residue), `flag` (genuine vocab gap), `no_target` (structural gap).
- **`adjudicate_content.py`** — the Tier-3 LLM adjudicator (Gemini 2.5 Flash, mirroring the extractor's LangChain + Pydantic + retry setup). Resolves the `review` tail, proposing `llm_suggested_mapped/_literal/_flag` with a confidence and rationale, constrained to the routed vocabulary.
- **`score_adjudication.py`** — evaluates the adjudicator against the human-adjudicated gold (`content_map_reviewed_*.json`): disposition agreement, IRI exact/overlap, confusion matrix, confidence buckets.

**Data flow:** matcher writes `content_map.json` (a de-duplicated worksheet, one row per distinct value) → human and/or adjudicator decide the `review` rows → decisions are preserved across re-runs.

---

## 3. Core design decisions

- **Deterministic-first, LLM-for-the-tail.** The matcher auto-disposes the bulk (data categories, measures, operations that lexically match); only genuinely undecided values reach the LLM. This keeps the expensive, error-prone component off the easy cases.
- **De-duplication on distinct values.** The worksheet has one row per distinct value with a `count`, so a decision is made once and applied to every occurrence. This is what makes the full corpus tractable — distinct (value, article) pairs *saturate* rather than growing with paragraph count.
- **Provenance re-keying `(value, article-root)`.** Rows are keyed on value **and** article root (`gdpr:art_9`), so the same value under different articles is separable, and every mapping carries the `source_article` needed to join back to the statement nodes in Neo4j (Stage 4 / FR5).
- **Status-based HITL, not confidence-gated.** Human decisions (`manually_*`) and LLM proposals (`llm_suggested_*`) are distinct statuses, preserved verbatim across matcher re-runs; only untouched rows regenerate. This operationalises the report's provenance requirement (distinguishing auto-ingested from expert-reviewed content) at row granularity.
- **Flag, don't force-fit.** A mappable concept with no vocabulary home becomes `flag` — a first-class output and a research finding — rather than being silently forced onto a near-miss.
- **Correctness first, evaluation second.** A per-slot "coverage" metric invented mid-project was explicitly abandoned once it began driving design choices; it was never in the report, and optimising it was corrupting the graph. Mapping correctness is the objective.

---

## 4. Strategies we tried and reverted (the instructive failures)

These belong in a limitations / methodology-honesty section — each was a plausible idea that the data falsified.

- **Object head-noun gate.** An attempt to auto-map objects only when the lexical hit covered the syntactic head noun. It over-fired, stalled legitimate single-concept objects into review, and made the worksheet confusing. **Reverted** to the simple rule "map every term above the IDF floor; else review."
- **Wide/uniform routing ("route everything").** Routing every slot to the whole regulation vocabulary. It did **not** fix the real problems (surface-form mismatches like *processors* ≠ `Data Processor` still failed), it added incidental co-emissions (`Processing` on objects), and it flooded the condition slot. **Reverted** to scoped routing.
- **Symmetric object+condition merge.** Merging both content slots to a shared vocabulary. Empirically, gold showed **zero** condition rows needing an object-side concept but **three** object rows needing condition-side concepts (`ScientificResearch`, `AutomatedDecisionMaking`, `PurposeIncompatible`). **Reverted** to an *asymmetric* routing: object gets the union, condition keeps its own scope.
- **Prompt-level rules against generic ride-alongs.** Instructing the adjudicator to avoid mapping incidental generics (`Law`, `Contract`, `Scope`). It **failed** — the model mapped `Law` at confidence 1.0. Replaced by a deterministic exclusion.
- **Confidence-thresholded escalation for the adjudicator.** The intended safety valve (escalate low-confidence rows). It **does not work** because the adjudicator's confidence is uncalibrated — it is *confidently wrong* (≈68–80% agreement even in the 0.9–1.0 bucket). This is the single most important negative finding (see §6, §7).

**Cross-cutting lesson:** deterministic post-passes and empirically-justified scoping consistently beat prompt guidance and clever gates. Where a judgment is genuinely context-dependent it belongs to the human/LLM; where it is structural it belongs in code.

---

## 5. Fixes and techniques that worked

- **Loading the DPV extensions.** The core-only vocabulary was missing concepts the human kept finding by hand. Loading **eu-gdpr** (`PurposeCompatible`, article-level bases, `data-breach` classes), **sector-health** (the medical purposes — directly relevant to MigrainePredict), and **justifications** (`DelayJustification`) closed real gaps and turned several "genuine gaps" into clean maps. Verified that a gap is only *genuine* once all relevant DPV modules are loaded.
- **Comprehensive routing assigned once, by content-kind.** Every DPV scheme was assigned deductively to predicate / object / condition (or excluded as non-content, e.g. `rules-classes` = the deontic meta-layer), so routing is complete *by construction*. After that, an unmapped value is a genuine finding, not a forgotten scheme — ending the reactive "add-a-scheme" churn.
- **Full-vocabulary adjudicator.** The adjudicator was capped by the matcher's top-k candidates (≈39% of its "errors" were the correct concept never being retrieved). Giving it the full routed vocabulary (validated against `terms.json`, so still no hallucination) removed that ceiling.
- **Data-justified deterministic exclusions.** Generics (`Law`, `Contract`, `Scope`, `RiskLevel`, `Justification`, `Proportionate`) and **article-code legal bases** (`A6-1-c`, `A9-2-h`, …) were excluded — after verifying the gold uses each of them **zero** times. Article codes were the largest precision leak; the citation is already carried by the extraction's `references`/`source_article` field, so mapping it again as a content IRI was pure duplication. This roughly quadrupled IRI exact-match.
- **Governed-verb guard.** `use a single assessment` (where "use" governs a determiner-led noun, not data) now dispositions `literal` deterministically and never reaches the adjudicator.
- **Negation preservation.** Stop-word stripping was deleting "not" from `Not Automated` → `{automated}`, inverting polarity and auto-mapping the opposite concept. Preserving negation words fixed it.
- **Manual/LLM decision preservation.** `preserve_manual` carries any `manually_*` / `llm_suggested_*` / `escalated` row across matcher re-runs (tolerant match on value, or value+article), so re-running to pick up routing/vocab changes never wipes adjudication.

---

## 6. The evaluation journey (honest arc)

The adjudicator was evaluated against the human gold set, scored **only on the rows it actually decided** (auto rows excluded so they can't inflate agreement).

| Stage | Disposition agreement | IRI exact | IRI overlap | Note |
|---|---|---|---|---|
| Initial (candidate-only) | ~68% | ~10% | — | ~39% of misses were retrieval-ceiling (concept never in candidates) |
| + full vocab, slot merge | ~72–81% | 5–10% | ~72% | retrieval + slot-blindness fixed; **new** problem: over-inclusion of generics/article-codes |
| + generic/article exclusions, polarity fix | ~78% | ~21% | **~89.5%** | precision recovered; remaining gap = defensible supersets + judgment calls |

**Decomposition of the final gap** (the useful finding): of the non-exact both-mapped rows, most are **supersets** — the adjudicator returns all the gold concepts plus a defensible extra (`ExplicitlyExpressedConsent` on an explicit-consent clause, `InnovativeUseOfNewTechnologies` on a new-tech DPIA trigger). A few are **genuine judgment calls** where the model is arguably right (`PurposeLimitationPrinciple` vs `PurposeCompatible`; the Art 5 principle vs the Art 32 measure). A few are **over-inclusion on long multi-purpose clauses** (the medicine bases) — precisely where human confirm adds value. None is a *systematic* error a further code change cleanly addresses.

**The hard ceiling: calibration.** Across every iteration the adjudicator's confidence never became informative — errors persist at 0.9–1.0 confidence. Consequently there is **no confidence band that can be auto-accepted**, so the LLM adjudicator is an **assistant** (a fast confirm pass: prune-an-extra, not research-from-scratch), **not an autonomous mapper**. The 89.5% concept-overlap is what makes that assistant genuinely time-saving; the calibration failure is what keeps the human in the loop.

---

## 7. Tie-back to the interim report

- **Confirms the plan.** The constrained-generation principle, ADAPT-stack grounding (FR2), Pydantic-validated structured output, and Galli's decomposition all held up: the matcher and adjudicator only ever choose from the routed vocabulary, and schema-drift (the Turaga weakness) never occurred.
- **Realises the HITL principle (FR4/NFR5) — with one important refinement.** The report specifies *confidence-thresholded routing* to expert review. Our empirical finding is that **LLM-adjudicator confidence is uncalibrated**, so a confidence threshold cannot be the HITL trigger for the mapping stage. The workable mechanism is **status-based**: deterministic disposition decides what is auto-accepted vs sent to review, and every `llm_suggested_*` row is confirmed by a human before entering the graph (regardless of its confidence). This is a genuine, defensible correction to the report's Section 5.1 / NFR5 for the mapping stage, and it strengthens the safety-first framing rather than weakening it.
- **Feeds the evaluation plan (Section 5.3).** The adjudicator-vs-gold scoring is an **inter-annotator-style agreement** metric (agreement with a single expert, framed as such), complementary to the report's extraction precision/recall and ontology-coverage (OOPS!) metrics. The gap decomposition (retrieval ceiling → adjudication quality → calibration) is itself a reportable result about where LLM-assisted ontology population succeeds and fails — directly relevant to a "Compliance Adherence Rate" narrative and to the limitations chapter.
- **Findings worth citing.** DPV cannot represent GDPR's **quantitative thresholds** (the 250-employee Art 30(5) exemption; the 72-hour Art 33 deadline) — surfaced as clean `flag`s. This is concrete evidence for the "extend existing ontologies" recommendation (Slide 11) and a candidate contribution.

---

## 8. Definition of done, and what's next

**The mapping stage is complete and defensible:**
1. **Deterministic matcher** — systematic error classes closed (generics, governed verbs, polarity, retrieval ceiling, slot alignment).
2. **Gold set (50 records)** — human-adjudicated to expert quality in `content_map_reviewed_1.json`; ready to load into Neo4j now.
3. **LLM adjudicator** — evaluated (~78% disposition, ~89.5% overlap, uncalibrated → assistive, not autonomous), with the failure profile decomposed.
4. **Residual disagreements** — characterised as defensible supersets + judgment calls + long-clause over-inclusion; documented as limitations, not open bugs.

**Next (higher-value than chasing exact-match from 21% upward):**
- **Stage 3/4 — Verification & Integration:** load the gold-set statements (subject/modality/predicate/object/condition IRIs + `references`) into Neo4j, with provenance labels. This turns the mapping effort into the actual knowledge graph.
- **Phase 2 — GraphRAG:** the NL-to-Cypher self-correcting query layer (Echenim pattern) over the populated graph — the end-to-end MigrainePredict demonstration.
- **Full-corpus mapping** — STARTED 2026-07-12; see §9 below.

---

## 9. Corpus scale-up (2026-07-12 — in progress)

Sections 1–8 describe the stage as built and evaluated on the 50-record eval
sets. This section is the running record of the same stage applied to the full
corpus extraction (`derived_actors` pipeline; 1,627 deontic records: 640 GDPR
+ 987 AI Act).

### 9.1 Step 1 — deterministic dry-run (DONE)

Zero LLM calls; counts only, per the agreed count-before-review-commitment
plan. Tooling changes to run it: `map_subject.py` / `map_modality.py` accept
bare `.jsonl` file paths (output written alongside the input);
`build_content_candidates.py` gained `--out` so the corpus worksheet never
touches the eval-set `mapping/content_map.json`.

**Subject mapping — FINAL as-is (Yoseph's decision, 2026-07-12).**
`mapping/subject_lexicon.json` was extended from 3 to 16 roles (every IRI
verified present in `mapping/vocab/terms.json`; aliases taken only from
observed corpus forms — the `build_subject_lexicon.py` audit workflow).
Result: **1,192 / 1,744 subject elements mapped (68%)**; the old 3-role
lexicon managed 44%. The 551 unmapped occurrences stay flagged
(`subject_unmapped`) and are not being fixed. Four residual classes:

1. **Real actors with no concept in DPV/AIRO/VAIR** (~200 occurrences):
   Member States (120), the Commission (37), national competent authorities
   (~30), the AI Office (12). A genuine vocabulary-gap finding, same family
   as §7's quantitative-threshold gaps. (`vair:EUOffice` was considered for
   the AI Office and rejected — its definition is just "EU office".)
2. **Composite subjects** (~50): "the controller or processor", "providers
   and deployers" — the resolver maps one value to one IRI.
3. **Non-actor subjects** ("the EU declaration of conformity", "the technical
   documentation"): extraction mis-attribution residue; extraction is frozen,
   so these correctly stay flagged.
4. **Referential long tail** at 1–7 occurrences each ("certification bodies
   referred to in paragraph 1").

One regulation-mismatch flag fired: "the controller" on
`aiact:art_59/par_1/pt_g` (a sandbox provision that genuinely addresses GDPR
controllers) — the cross-regulation guard working as designed, left flagged.

**Modality mapping — FINAL: 1,627 / 1,627 mapped.**

**Content slots** (candidate builder, tag pass active — its first production
use, per §5). Worksheet: `mapping/content_map_corpus.json`, seeded from
`content_map_reviewed_2_conflict_pair.json` (a strict superset of
`content_map_reviewed_2.json`) so the 40 locked eval-scale decisions —
human-adjudicated rows and the conflict-pair anchors — carry over via
`preserve_manual`. Dispositions:

| slot / reg | distinct (occurrences) | auto-mapped | review | literal | no_target |
|---|---|---|---|---|---|
| predicate gdpr | 479 (677) | 130 | — | 348 | — |
| predicate aiact | 788 (1,059) | — | — | — | 788 |
| object gdpr | 641 (715) | 419 | 211 | — | — |
| object aiact | 1,102 (1,165) | 671 | 430 | — | — |
| condition gdpr | 438 (507) | — | 93 | 320 | — |
| condition aiact | 631 (747) | — | 390 | 239 | — |

Findings from the counts (documented, not fixed):

- **AI Act predicates are 100% `no_target` (788 distinct):** the routing
  table has no scheme for (predicate, aiact) because AIRO/VAIR contain no
  action/process concepts. Structural vocabulary gap; those predicates stay
  literal text in the graph.
- **§3's "distinct values saturate" claim is falsified at corpus scale** —
  it held on the eval sets but distinct/occurrence ratios at corpus are
  0.71–0.95 (most values appear once), so de-duplication saves only ~15–30%
  and review volume grows roughly linearly with corpus size.
- Tag pass promoted 6 rows literal → review (all GDPR conditions, incl. the
  storage-limitation and data-minimisation principles — the §5 design working
  in production).

**Files:** `data/{gdpr,aiact}.subject_mapped.jsonl`,
`data/{gdpr,aiact}.modality_mapped.jsonl` (chain outputs, regenerable),
`mapping/content_map_corpus.json` (the corpus worksheet — carries decisions,
durable), `mapping/subject_lexicon.json` (extended, durable).

### 9.2 Step 2 — adjudication of the full review queue (DONE 2026-07-12)

Review-budget decision (Yoseph, 2026-07-12): adjudicate ALL 1,124 review rows,
then human-confirm only a scenario-prioritised slice; findings documented.

**Run:** `adjudicate_content.py --path mapping/content_map_corpus.json`
(Gemini 2.5 Flash, propose-only, candidate/vocabulary-constrained). All 1,124
rows adjudicated, 0 left in review; transient 503/429 spikes absorbed by the
retry logic; the 40 seed-locked rows verified untouched. Proposals:

| proposed status | rows |
|---|---|
| `llm_suggested_mapped` | 859 |
| `llm_suggested_flag` (no vocabulary home) | 140 |
| `llm_suggested_literal` | 124 |
| `escalated` | 1 |

The single escalation is the out-of-vocabulary guard working: an object value
("a template") the model could not ground in the routed vocabulary. NOTE these
are PROPOSALS — nothing here is accepted; acceptance is the human confirm pass
(edit `llm_suggested_*` → `manually_*`), per §6's calibration finding.

**Confidence is still uncalibrated, now at corpus scale:** 95% of proposals
carry confidence ≥ 0.9 (519 rows at exactly 1.0); only 1 row fell below the
0.7 escalation threshold. Same behaviour as the eval-scale measurement — the
model almost never says "unsure", so confidence cannot gate acceptance and the
HITL trigger stays status-based.

**The confirm slice: 99 rows.** Definition (grounded in what the evaluation
actually exercises, not chosen ad hoc): rows whose article root is cited by
the adopted gold-50 queries OR is a planned CONFLICT_PATTERNS anchor — 18
article roots (gdpr: art_2, 4, 5, 9, 16, 17, 22, 25, 28, 30, 33, 34, 35;
aiact: art_3, 9, 10, 12, 14). Slice composition: 84 proposed-mapped, 14
proposed-literal, 1 proposed-flag.

**Finding — the healthcare-relevance gate cannot define the slice:** 1,523 of
the ~2,536 corpus records (60%) are `applies_to_healthcare=true`, spanning 259
article roots, because MigrainePredict genuinely triggers most provisions of
both regulations. "Healthcare-relevant" therefore does not discriminate;
gold-cited + conflict-anchor articles is the criterion that does.

### 9.3 Step 3 — human confirm sample + apply-back (DONE 2026-07-12)

**Confirm pass (Yoseph): a 9-row random sample from the slice articles, and
that is the entirety of human review at corpus scale** (his decision — not a
full 99-row pass). All 9 are `manually_mapped`, all inside slice articles.
Composition worth naming: 7 of the 9 were rows the deterministic matcher had
left as `literal` (so they were never adjudicated — the §1.1/anchor-B
under-mapping class in the literal tail), 1 corrected/confirmed an auto-map,
and 1 adopted an adjudicator proposal (the Art 17(3) archiving condition →
`dpv:PublicInterest` + `dpv:ScientificResearch`). His sample includes the
flagship b-side anchor: `gdpr:art_5/par_1/pt_e`'s condition →
`eu-gdpr:StorageLimitationPrinciple`.

**Disposition of everything else (Yoseph's decision): all remaining
`llm_suggested_*` proposals — the rest of the 99-row slice and the 1,025
non-slice rows — enter the graph AS PROPOSALS, carrying their status.**
`mapping_status` rides on every slot element into the records (and
`load_candidates.py` turns any `llm_suggested_*` slot into an `:LLMSuggested`
statement label), so unvetted content is queryable, filterable, and honest —
never silently trusted. Consequence to report: at corpus scale the
human-verified layer is the 9-row sample plus the 40 carried eval-scale
decisions; everything else is deterministic-match or labelled LLM proposal.

**Apply-back:** `map_content.py` (extended to accept bare file paths, like
the rest of the chain) with `--reviewed mapping/content_map_corpus.json` over
the modality-mapped corpus → `data/{gdpr,aiact}.content_mapped.jsonl`, all
2,536 records. Slot-element outcomes: predicate 250 mapped / 426 literal /
1,059 no_target (the §9.1 AI Act predicate gap) / 11 unmatched; object 1,177
mapped + 416 llm-proposed + 147 llm-flag; condition 638 literal + 571
llm-proposed + 15 manually mapped. **The 11 unmatched are all empty-string
predicates — the extraction stage's documented blank-slot class
(`gdpr:art_84/par_1` among them) surfacing exactly where it should; each is
flagged `content_unmatched` + `needs_review`, not guessed.** Spot-checked:
both flagship conflict anchors are live in the output (`vair:LoggingMeasure`
on aiact:art_12/par_1; `eu-gdpr:StorageLimitationPrinciple` on
gdpr:art_5/par_1/pt_e).

### 9.4 Next

The corpus mapping stage is CLOSED. Next stage is load → verify at corpus
scale (`scale_up_readiness.md` §2: re-derive the generic-concept tier,
sample-check duplicate thresholds; §4.1: CONFLICT_PATTERNS expansion — both
flagship anchors confirmed mappable above).

## 10. Reusable methodological principles that emerged

1. **Deterministic post-passes over prompt guidance** — structural problems get code fixes; prompt rules under-perform and don't hold under high model confidence.
2. **Ground every design choice in the gold data** — every exclusion and routing decision here was justified by measured gold usage, not intuition (and several intuitions were falsified).
3. **Separate retrieval from adjudication when evaluating** — conflating "the concept was never offered" with "the model chose wrong" hides the real bottleneck.
4. **Uncalibrated confidence ⇒ status-based HITL, not threshold-based** — the safety mechanism must not assume the model knows when it's wrong.
5. **A measured, decomposed negative result is a contribution** — "where and why LLM-assisted mapping needs a human" is a publishable finding, not a failure.