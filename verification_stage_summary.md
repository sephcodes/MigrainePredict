# Verification Stage — Engineering Retrospective

*Phase 1, "Verification" sub-stage of the MigrainePredict compliance-KG pipeline (Extraction → Mapping → **Verification** → Integration).*

This document summarises the strategies, dead ends, fixes, and decisions from the verification work, and ties them back to the interim report. It is written to feed the dissertation's **methodology**, **implementation**, and **limitations/evaluation** sections, in the same format as the mapping-stage retrospective.

---

## 1. What this stage is, and where it sits

Mapping produced statements whose subject/modality/predicate/object/condition slots are grounded to ADAPT-stack IRIs. This stage checks those statements **against each other** before integration, per the interim report's Stage 3 (§5.3.1): **contradiction detection** (opposite deontic forces on the same subject and predicate–object), **redundancy detection** (multiple statements expressing the same constraint), and **cross-regulation conflict detection** (curated patterns, flagship AI Act Art 12 logging vs GDPR Art 5(1)(e) storage limitation). Problematic statements are flagged and held out of the auto-ingested graph (FR5); everything else is marked verified.

**Input:** the canonical run (run4 — best of five on the COMBINED-50 extraction score: P 0.891 / R 1.000 / F1 0.942 / F2 0.976) of both content-mapped sets, DEV + HOLDOUT = 51 statements (33 deontic, 8 definitional, 10 applicability).

---

## 2. The pipeline we built

Three components plus a scorer, executed in order:

- **`load_candidates.py`** — MERGE-idempotent loader into a Neo4j staging graph: `(:Statement:Candidate)` nodes; slot edges (`HAS_SUBJECT/PREDICATE/OBJECT/CONDITION`) to `(:Concept)` nodes carrying `mapping_status` + `method` **on the edge** (slot-granular provenance); `(:Provision)` nodes with `SOURCED_FROM`/`REFERS_TO` edges from the extraction's references (statement-level `#` refs resolve to statement nodes); a `BROADER` concept hierarchy parsed from the mapping stage's `terms.json` for subsumption-aware matching. Statements with any `llm_suggested_*` slot get `:LLMSuggested`, any `manually_*` slot `:HumanReviewed` (FR6 provenance).
- **`verify_statements.py`** — the three checks as parameterised Cypher pattern-matches (the report's §5.4 design), writing typed relationships into the graph (`EXCEPTION_OF`, `CANDIDATE_CONTRADICTION`, `SPECIALISES`, `REDUNDANT_WITH`, `CONFLICTS_WITH`), setting `verification_status = verified | flagged` on every candidate, and emitting a replayable verdict JSONL. Includes the synthetic flagship conflict pair (below) behind a `--no-synthetic` switch.
- **`make_verification_worksheet.py`** — the HITL/evaluation worksheet: every surfaced pair plus all *near-miss* negatives (pairs with at least one evidence signal that did not fire), with both statements' anchor text and conditions inline, and human decisions preserved across regeneration (the mapping stage's status-based-HITL pattern).
- **`score_verification.py`** — detector precision/recall and agreement against the human-labelled worksheet.

**Verification persists in the graph itself:** the checks are not a report generated on the side — their outcomes are graph state (typed edges + `verification_status`), which is what Phase 2 queries will consume.

---

## 3. Core design decisions

- **Staging-graph + Cypher, report-faithful.** The report specifies "Cypher pattern-matching queries over the candidate-statement set, executed before integration". We load all candidates into Neo4j first and verify in place. Consequence: the staging graph **is** the knowledge graph — Stage 4 reduces to filtering on `verification_status` plus the audit log, rather than a second ingestion.
- **Ingest-with-provenance HITL.** `llm_suggested_*` mappings do not block verification; they enter the graph carrying provenance labels, reviewable at any time. (Confirmed decision, consistent with the mapping stage's uncalibrated-confidence finding: the gate is status, not confidence.)
- **Flag, never drop.** No check destroys a statement. Contradiction/duplicate/conflict members are flagged for review; exception structures and specialisations are recorded as *informational* typed edges — positive findings, not defects.
- **Synthetic flagship conflict.** Neither side of the report's named cross-regulation conflict (AI Act Art 12(1), GDPR Art 5(1)(e)) is in the mapped subset, so check 3 is demonstrated on an injected `:Synthetic` pair modelled on the report's §5.5 worked example (anchored on `vair:LoggingMeasure` vs `eu-gdpr:StorageLimitationPrinciple`). Real-data demonstration is deferred to the full-corpus scale-up — the same call made for full-corpus mapping.
- **The labelling standard is pair-local ("prima facie"), and resolution is computed, not judged.** The single most consequential decision (see §5): a human labels whether two statements clash *on the face of their own text*; whether the corpus elsewhere resolves the clash is a graph traversal (the resolution pass), not an annotation judgment.

---

## 4. Strategies we tried and reverted (the instructive failures)

The first-round detector was evaluated against a fully hand-proposed, human-adjudicated worksheet (82 pairs) and failed informatively:

- **IRI-overlap contradiction detection: precision 0/20, recall 0/4.** The rule "opposite polarity + shared subject + predicate and object concept overlap" produced twenty false alarms forming two hubs — every pair reduced to *controller × `dpv:Processing` × `dpv:PersonalData`*, concepts so ubiquitous that overlap on them carries no information. Simultaneously it **missed all four genuine tensions** (processing obligations vs Art 9(1)'s unconditional prohibition) because their predicates had remained literal text at mapping (no IRI to overlap). One root cause — mapped IRIs are coarser than the norms — producing both failure modes at once.
- **Concept-subsumption specialisation: precision 0.38.** Detecting "one statement refines another" via object-concept subsumption produced sibling-pair artifacts (Art 32(1)(a) vs (c) "specialising" each other through shared `PersonalData`) and missed Art 32(1)(c) entirely. Reverted in favour of a structural rule.
- **Reference-linkage alone over-classifies exceptions.** Art 30(5) *references* Art 9(1) only as a condition ("unless … special categories"), which is structurally indistinguishable from a derogation reference; the round-1 classifier called it an exception structure. In gold, all true exception structures are same-article; the rule was scoped accordingly.
- **Object-IRI-equality duplicates: 3/3 false alarms.** Distinct norms (lawfulness vs purpose-specification; the Art 9(2)(h) vs (j) legal bases) collapse to identical concept sets. Equality of mapped IRIs is evidence of *vocabulary granularity*, not of redundancy.
- **"Unresolvable contradiction" as the labelling standard — abandoned before use.** The natural definition of contradiction ("cannot be resolved") is not human-judgeable: an annotator would need the entire corpus by heart, and labels would silently rot as the corpus grows. Replaced by the prima facie standard (§3). The successor risk — **gold co-refined with the detector** — materialised and was caught by human review (§5): after relabelling to the consistent standard, the detector briefly scored a suspicious 100%, and the expert pass then overruled four of those labels.

**Cross-cutting lesson (mirrors the mapping stage):** structure-driven rules (explicit references, paragraph hierarchy, unconditional-prohibition form) consistently beat similarity-driven rules (concept overlap). Where the graph encodes legal structure, detection is near-perfect; where a rule leans on IRI similarity, it inherits the vocabulary's coarseness.

---

## 5. Fixes and techniques that worked

- **Discriminative-overlap gate.** A data-derived generic tier — concepts appearing on ≥10% of deontic candidates (`PersonalData`, `Processing`, `GDPRRightsImpact`, `ProcessingContext`, `MakeAvailable`) — whose members cannot serve as an overlap witness. Removed all twenty round-1 false alarms; the relative threshold transfers to corpus scale.
- **Unconditional-prohibition rule.** A prohibition with an *empty condition slot* (Art 9(1) is the corpus's only real one) versus an obligation/permission whose object concepts stand in a subsumption relation fires **without requiring predicate IRIs** — closing the literal-predicate miss class.
- **Structural specialisation.** Same modality + shared subject + one statement's paragraph a `pt_*` child of the other's (chapeau → sub-point). 4/4 on gold including the previously missed Art 32(1)(c), zero sibling artifacts.
- **Same-article exception scoping and tightened duplicates** (predicate-set equality + condition-token compatibility): removed the remaining round-1 false positives; duplicate firings on real data went to zero.
- **The resolution pass.** A detected tension whose prohibition carries an incoming `EXCEPTION_OF` derogation is marked `resolved_via_exception` and does **not** enter the review queue. All thirteen detected tensions — the Art 9(1) "hub", which is legally real: every processing norm prima facie collides with the one unconditional prohibition until a 9(2) basis covers it — resolved mechanically through the three 9(2) exception edges already in the graph. **Real-statement review queue: zero of 51.**
- **Expert review as the final arbiter.** The human pass overruled four detector firings (Art 33, Art 34, Art 32(1)(c), Art 35 vs Art 9(1)): in each, `dpv:PersonalData` sits in the object list only because the *noun phrase* mentions it ("the personal data **breach**", "an assessment of the impact … on the protection of personal data") — the obliged act does not operate on the data. The schema records which concepts appear in a slot but not their **role** (operand of the act vs embedded mention) — the residual, named false-positive class.

---

## 6. The evaluation journey (honest arc)

Detector vs human-labelled worksheet (all surfaced pairs + near-miss negatives; recall relative to that pool, zero-evidence pairs assumed true negatives):

| Stage | Agreement | Tension P | Tension R | Exception P/R | Specialisation P/R | Duplicate FPs | Review queue (real) |
|---|---|---|---|---|---|---|---|
| Round 1 (naive detector) | 59.8% | 0.00 (0/20) | 0.00 (0/4) | 0.83 / 1.00 | 0.38 / 0.75 | 3 | 14 of 51 |
| Round 2 (refined, pre-review) | (100% — co-refined, see below) | — | — | — | — | 0 | 0 of 51 |
| **Final (expert-reviewed)** | **95.5%** | **0.69 (9/13)** | **1.00 (9/9)** | **1.00 / 1.00** | **1.00 / 1.00** | **0** | **0 of 51** |

The round-2 100% is reported only to be discounted: the gold was co-refined with the detector (labels re-aligned to the consistent prima facie standard), so the score partly reflected self-agreement. The expert pass is what converts it into a result — it confirmed the standard *and* overruled four firings, yielding the honest 0.69 precision with a single named residual cause (operand-vs-mention). Methodologically this is annotation-guideline iteration: pilot labels → disagreement → refined guideline → re-label → independent adjudication.

**The scaling answer.** Flags-per-statement went 0.27 → 0. The review queue at corpus scale grows with *unresolved* tensions and genuine vocabulary gaps — the things a human should see — because prima facie tensions are absorbed mechanically by the exception structures extracted from the regulation itself. Additionally, expert decisions compress: the 13 tension pairs share one hub statement (Art 9(1)) and were adjudicated as one grouped judgment plus a per-row misfit scan.

---

## 7. Tie-back to the interim report

- **Confirms the plan.** Stage 3's three checks were implemented exactly as §5.4 specifies — Cypher pattern-matching over the candidate set, executed before integration; conflicts routed to review and held out of auto-ingest (FR5); the curated cross-regulation pattern table with the Art 12 / Art 5(1)(e) flagship (FR4). The Echenim gap — deontic conflict resolution flagged as future work — is partially addressed: prima facie conflicts are detected *and* mechanically resolved where the regulation's own derogations cover them.
- **One deviation to write up: OOPS! was superseded, not skipped.** §5.4 proposed the OOPS! pitfall scanner for redundancy. OOPS! audits *ontology schemas* (class hierarchies, definitions); it was apt when the plan risked LLM-authored classes. The final pipeline reuses DPV/AIRO/VAIR wholesale and authors no classes, so OOPS! would audit the ADAPT Centre's ontologies rather than this project's contribution, and it has no visibility into statement-instance redundancy — which is what Stage 3 actually required and what the typed-edge redundancy check provides. The dissertation should state this as a consequence of the reuse-not-author decision (NFR3).
- **Refines §5.1/NFR5 again, consistently with the mapping stage.** The report's confidence-thresholded routing is replaced here by *structure-based* routing: what reaches a human is decided by deterministic graph structure (unresolved tension, duplicate, conflict), not by a model's self-reported confidence. Together with the mapping stage's status-based HITL, the pattern is uniform across the pipeline: **the safety mechanism never assumes the model knows when it is wrong.**
- **Feeds the evaluation plan (§5.5).** The worksheet is an expert-adjudicated gold in the report's real-plus-synthetic style (Chattoraj pattern): real pairs plus an injected synthetic conflict; failure modes decomposed into named causes (generic-overlap hub effect; literal-predicate misses; operand-vs-mention residual). The 59.8% → 95.5% arc with causes is the reportable result.
- **Findings worth citing.** (i) Structure-driven verification outperforms similarity-driven verification on legal text — the graph's explicit reference and hierarchy edges are the signal. (ii) Slot-level IRI grounding cannot express whether a concept is the act's *operand* or a *mention* inside a noun phrase — a concrete, evidenced granularity limit of statement-level ADAPT grounding, alongside the mapping stage's quantitative-threshold gaps. (iii) Art 9(1) as a "legally real hub": prima facie tension detection recovers the regulation's own architecture (general prohibition + enumerated derogations) as graph structure.

---

## 8. Definition of done, and what's next

**The verification stage is complete and defensible:**
1. **Three checks implemented** as parameterised Cypher over the staging graph, outcomes persisted as graph state.
2. **Expert-adjudicated gold** (89 pairs, labels final) with the scorer; final metrics as in §6.
3. **Residual failure class** named and bounded (operand-vs-mention, 4 pairs); candidate deterministic fix (object head-noun analysis) deliberately not built — four rows do not justify new machinery (the mapping stage's abandoned head-noun gate is the cautionary precedent).
4. **Review queue empty** for the 51-statement graph; all prima facie tensions resolved by in-graph exception structures.

**Next:**
- **Stage 4 (integration) — mostly subsumed.** The staging graph is the knowledge graph: provenance labels (`:LLMSuggested`/`:HumanReviewed`), `verification_status`, and typed relations are already in place. Remainder: an audit-log JSONL (NFR1: one timestamped event per load/verdict/flag) and a `:Verified` convenience label for Phase 2 filtering.
- **Phase 2 — GraphRAG:** the NL→Cypher self-correcting query layer over this graph; the `EXCEPTION_OF` and `CONFLICTS_WITH` edges become traversable evidence for verdict synthesis (the §5.5 worked example's dispensation reasoning).
- **Full-corpus scale-up (later):** re-run load → verify → resolve at corpus size; review only unresolved flags; extract + map the real Art 12 / Art 5(1)(e) pair to replace the synthetic conflict.

---

## 9. Reusable methodological principles that emerged

1. **Structure beats similarity.** References, paragraph hierarchy, and deontic form (unconditional prohibition) are near-perfect signals; concept-overlap similarity inherits every granularity defect of the vocabulary.
2. **Make the annotator's judgment pair-local; compute the rest.** A labelling standard must be decidable from what the annotator can see. "Unresolvable corpus-wide" is not; "prima facie, from these two statements" is — and resolvability becomes a graph query.
3. **Ground thresholds in the data.** The generic-concept tier is a measured frequency floor (≥10% of statements), not intuition — the same discipline as the mapping stage's gold-justified exclusions.
4. **Co-refined gold is not gold until independently adjudicated.** Refining guidelines and detector together is legitimate and productive, but the score it produces is self-agreement; the expert pass both validated the standard and found the four errors that define the residual failure class.
5. **An empty review queue is a result, not an absence.** Showing that all detected tensions are absorbed by the regulation's own derogation structure is the scaling argument for HITL verification — humans see only what the graph cannot answer.
