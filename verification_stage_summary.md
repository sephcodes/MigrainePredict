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

## 9. Corpus scale-up (2026-07-12/13 — in progress)

Sections 1–8 describe the stage as built and evaluated on the 51/54-statement
eval graph. This section is the running record of the same stage applied to
the full content-mapped corpus (2,271 non-NA statements: `derived_actors`
extraction → corpus mapping, see `mapping_stage_summary.md` §9).

### 9.1 Procedure and decisions (Yoseph, 2026-07-13)

- **The corpus replaces the eval graph in Neo4j** (`load_candidates.py
  --wipe`). Loading alongside would duplicate the same provisions with
  differently-worded statements. The 54-statement eval graph remains
  reproducible from tracked repo files with the documented one-command
  rebuild; its audit log is preserved as
  `data/verification/audit_log_evalgraph54.jsonl`.
- **Conflict-pattern table: flagship only for the first run** (logging vs
  storage limitation). The three additional patterns are designed against the
  measured graph afterwards, not guessed up front.
- **Corpus-named outputs:** verdicts `data/verification/corpus.verification.jsonl`;
  review worksheet path `data/verification/verification_reviewed_corpus.json`
  (deliberately fresh — the eval worksheet's dispositions are keyed by
  statement-id pairs, and corpus ids share the same scheme, so reusing it
  could apply old human labels to different statements).

### 9.2 Load result

2,271 statements (987-record GDPR file + 1,549 AI Act, NA stubs skipped),
5,347 slot edges, 551 concepts, 1,718 provisions, 486 BROADER edges.
Provenance verified in the graph: 825 statements carry `:LLMSuggested`
(≥1 LLM-proposed slot), 17 `:HumanReviewed` (≥1 human-mapped slot) — the
mapping stage's ingest-with-provenance decision needs no extra machinery.

### 9.3 First verification run — results and the two problems it measured

Verdict tally (~25,000 pairs evaluated): 1,024 candidate contradictions (215
auto-resolved via exception structures → 809 unresolved), 119 exception
structures, 351 specialisations, 26 duplicate candidates, 230 duplicate
definitions (dominated by recitals re-defining Art 4 / Art 3 terms — the
known recital-fragmentation extraction class), 2 cross-regulation conflicts.
**471 statements (20.7%) flagged and held out of `:Verified`** — composed of
299 statements from unresolved tensions, 180 from duplicates, 3 from
conflicts.

**The flagship conflict fired on real corpus statements**
(`aiact:art_12/par_1#s1` × `gdpr:art_5/par_1/pt_e#s1`). A second firing —
× `gdpr:art_47/par_2/pt_d#s1` — is a measured instance of the
operand-vs-mention class (§8, deferred fix): the BCR provision requires a
*document to list* "limited storage periods", which maps the
storage-limitation anchor without imposing the duty.

**Problem 1 — the generic-tier assumption failed (checklist §2.1 predicted
this needed re-validation).** The ≥10% tier computed over the pooled corpus
collapsed to `{airo:AISystem}`: the denominator mixes both regulations whose
vocabularies are disjoint, so `dpv:PersonalData` (7.9% pooled, but 20% of
GDPR's own deontic statements) fell under the cliff. The eval-scale claim
"the relative threshold transfers to corpus scale" is falsified.

**Problem 2 — the tension review queue is unmanageable (809 pairs), and the
tier is not the main cause.** Measured decomposition of the 809: **692 fire
with a discriminative witness that is frequency-rare but semantically
empty** — e.g. Annex IV *documentation* obligations ("the technical
documentation shall describe the system's capabilities") vs Art 5 *prohibited
practices*, witnessed by `airo:AICapability ~ vair:ImageRecognition`
subsumption; documentation about a capability is not exercise of it
(operand-vs-mention at scale). **117 fire only through rule_b** (unconditional
prohibition + raw object overlap), which was deliberately not gated by the
generic tier at eval scale — so `airo:AISystem = airo:AISystem` identity
matches pass. Top tension hubs: the four Art 21 (right-to-object) statements
(~346 pair-slots) and the AI Act Art 5 prohibited-practices statements.

### 9.4 Fix 1 — per-regulation generic tier (approved, validated, kept;
did NOT reduce the queue)

`GENERIC_QUERY` now derives the tier per regulation (a concept is generic if
on ≥10% of *its own regulation's* deontic candidates): corpus tier =
`{dpv:PersonalData, dpv:MakeAvailable, airo:AISystem, airo:Regulation}`.
**Validation per the standing rule: the eval graph was rebuilt and re-verified
under the changed query — all 208 verdict pairs digit-identical to the
documented eval verdicts, 0 flagged, reviewed dispositions re-applied.** On
the corpus the tally was unchanged (1,024 / 215 / 471): both measured noise
classes bypass the gate the tier feeds (§9.3 problem 2). The fix is kept as
correct in its own right; the honest record is that it addressed the tier
artifact, not the queue.

### 9.5 Fix 2 — witness informativeness gate (TRIED, FAILED VALIDATION, REVERTED)

Decision (Yoseph, 2026-07-13): the §8 deferred-fix trigger ("build it if the
corpus review queue is unmanageable") has been met, so we tried gating rule_b's
object overlap through the discriminative (non-generic) check — the same gate
rule_a already uses — to kill the generic-identity-witness tensions
(`airo:AISystem = airo:AISystem`).

**Scope measured before building (honest ceiling):** of the 809 unresolved
tensions, only **117** fire *without* a discriminative witness (the rule_b
generic-identity class); the other **692** already carry a discriminative
witness and would survive this gate. So the best case was 809 → 692 tensions
and ~112 statements freed — a partial fix, never the whole queue.

**It failed the standing digit-identical eval-replay check and was reverted.**
Rebuilding the 54-statement eval graph and re-verifying under the gate flipped
**14 verdicts** — every Art 9(1)-hub tension (Art 16/17/32/33/34/35 vs
Art 9(1)) went `candidate_contradiction` → `none`. Cause: the GDPR Art 9(1)
hub — the eval stage's flagship "legally real hub" finding (§5, §7) — fires
through rule_b on a **generic** witness (`dpv:PersonalData` /
`dpv:Processing`), because the colliding obligations operate on personal data
generally while only Art 9(1) carries the special-category concept. The gate
that removes the corpus garbage also erases the legitimate GDPR hub. (Flagged
count stayed 0 at eval scale — those tensions were all resolved-via-exception
anyway — but the verdicts changed, so it is not digit-identical and the
documented hub finding would evaporate.)

**The real lesson (why a frequency gate cannot fix this):** the *same*
mechanism — rule_b's raw object overlap on an unconditional prohibition —
produces the legitimate GDPR Art 9(1) hub at eval scale and the AI Act Art 5
garbage at corpus scale, and *both fire on a generic identity witness*. What
distinguishes them is not concept frequency but **operand-vs-mention**: in
Art 9(1) both statements' acts genuinely operate on the data; in
Art 5-vs-Annex-IV the documentation obligation merely *mentions* the AI system
it must document, it does not *perform* the prohibited practice. Frequency
cannot see that difference — only the deferred object-head-noun analysis
(§8, §1.2 of `scale_up_readiness.md`) can. Code reverted to known-good
(eval replay digit-identical, corpus back to 471 flagged); the gate is not
kept.

### 9.6 Decision — push everything into the graph and measure (Yoseph, 2026-07-13)

Holding 471 statements out for "review" is not a real outcome — nobody reviews
471 statements, and hiding a fifth of the graph from Phase 2 is a cost with no
established benefit. We do not actually know that the tension/duplicate noise
degrades query answers. So the decision is to **stop gating visibility on the
detectors and test the question empirically.**

`verify_statements.py --no-holdout`: every statement keeps `:Verified` (all
2,271 queryable by Phase 2); the detector still runs and still writes every
typed edge — `CONFLICTS_WITH` (the flagship conflict edge is intact,
`aiact:art_12/par_1#s1` × `gdpr:art_5/par_1/pt_e#s1`), `CANDIDATE_CONTRADICTION`,
`EXCEPTION_OF`, `SPECIALISES`, `REDUNDANT_WITH` — and the 471 detector-flagged
statements are marked `detector_flagged = true` as a queryable property, but
flagging no longer removes `:Verified`. Graph state: 2,271 / 2,271 verified,
471 carry `detector_flagged`.

The operand-vs-mention noise (§9.3–9.5) is therefore *in* the graph, labelled,
not fixed and not hidden. **What the Phase-2 gold-50 rerun actually measures
(corrected framing):** not "do the tension edges hurt" — Phase-2 retrieval
follows `REFERS_TO | EXCEPTION_OF | CONFLICTS_WITH` only, never
`CANDIDATE_CONTRADICTION`, so the 809 tension edges are inert for queries. The
reason `--no-holdout` matters is *coverage*: the 471–473 detector-flagged
**statements** (Art 21, the AI Act Art 5 prohibitions, etc.) are real content
queries need, and holding them out would create answer gaps. The rerun
therefore measures corpus-scale retrieval precision, the unvetted
`llm_suggested` mapping concepts, and coverage gains (the predicted Q25/S3
closures) — the tensions are a verification-stage artifact, largely orthogonal
to query answers.

### 9.7 Conflict-pattern expansion (2026-07-13) — 2 genuine tensions added, 1 deferred as not-a-conflict

The `CONFLICT_PATTERNS` table (§4.1 of `scale_up_readiness.md`) held only the
flagship. Anchor mappability — the prerequisite — was confirmed at mapping
close, so two more **genuine cross-regulation tensions** were added, with
anchor concept lists verified present on the real corpus statements:

- **`special_category_prohibition_vs_bias_detection`** (Art 9(1) GDPR ↔
  Art 10(5) AIA): GDPR prohibits special-category processing; the AI Act
  permits it for bias detection. a-side = AIA PERMISSION anchored on
  `vair:BiasDetection`; b-side = GDPR PROHIBITION anchored on the
  special-category concepts (`pd:Health/Biometric/Genetic/Sexual`). **Fires
  exactly 1 pair — `aiact:art_10/par_5#s2 × gdpr:art_9/par_1#s1` — the
  intended pair, no noise.** Citation: van Bekkum & Zuiderveen Borgesius CLSR
  2023 + van Bekkum CLSR 2025 (peer-reviewed, pair-specific).
- **`erasure_vs_log_retention`** (Art 17 GDPR ↔ Art 12 AIA): erasure duty vs
  log-retention duty. a-side = AIA logging OBLIGATION (`vair:LoggingMeasure`);
  b-side = GDPR erasure OBLIGATION (`dpv:Erase`). **Fires 9 pairs**, all
  against the single logging statement `aiact:art_12/par_1#s1`: 8 are Art 17
  erasure (its main statement + 7 sub-point grounds — granular but each is a
  genuine erasure ground vs retention), 1 is Art 5(1)(d) accuracy-erasure
  (defensible). **One firing is an operand-vs-mention FP:**
  `gdpr:art_15/par_1/pt_e#s2` — the right-of-access provision *mentions* the
  erasure right (so it carries `dpv:Erase`) but is not itself an erasure duty.
  Same documented class as the Art 47 BCR flagship FP. Citation:
  Fosch-Villaronga, Kieseberg & Li CLSR 2018 (general tension; the AI-Act pair
  is an operationalisation).

Total conflict firings now 12 (flagship 2, special-category 1, erasure 9).

**The 4th planned pattern — Art 25 GDPR ↔ Art 10 AIA — was NOT added, on
purpose.** §4.1 itself classes it as *"parallel-obligation composition, not
conflict"*: both regulations impose compatible design/governance duties, they
do not pull in opposite directions. The current `CHECK3` mechanism only emits
`conflict` verdicts / `CONFLICTS_WITH` edges, so forcing this pair through it
would assert a conflict that the law (and the cited literature) does not.
**Open decision for Yoseph:** model parallel obligations with a separate
relation (e.g. `COMPOSES_WITH`, informational like `SPECIALISES`), or leave
Art 25↔10 out of the conflict layer entirely.

**Citation integrity (unchanged rule):** every pattern now carries a
`citation` field, but these are the CANDIDATE sources located during planning
— they require Yoseph's read-and-confirm before the write-up cites them, and
where a source supports the general tension but not the exact article pair the
pattern is framed as an operationalisation, not an independently-derived
mapping.

### 9.8 Next step

The Phase-2 gold-50 rerun against this graph (per §9.6's corrected framing:
it measures corpus retrieval + mapping noise + coverage, not the tension
edges). Pipeline-Gemini + pipeline-Mistral + vector baseline.

## 10. Reusable methodological principles that emerged

1. **Structure beats similarity.** References, paragraph hierarchy, and deontic form (unconditional prohibition) are near-perfect signals; concept-overlap similarity inherits every granularity defect of the vocabulary.
2. **Make the annotator's judgment pair-local; compute the rest.** A labelling standard must be decidable from what the annotator can see. "Unresolvable corpus-wide" is not; "prima facie, from these two statements" is — and resolvability becomes a graph query.
3. **Ground thresholds in the data.** The generic-concept tier is a measured frequency floor (≥10% of statements), not intuition — the same discipline as the mapping stage's gold-justified exclusions.
4. **Co-refined gold is not gold until independently adjudicated.** Refining guidelines and detector together is legitimate and productive, but the score it produces is self-agreement; the expert pass both validated the standard and found the four errors that define the residual failure class.
5. **An empty review queue is a result, not an absence.** Showing that all detected tensions are absorbed by the regulation's own derogation structure is the scaling argument for HITL verification — humans see only what the graph cannot answer.
