# Scale-up readiness checklist

**STATUS (2026-07-12): corpus EXTRACTION is done; corpus MAPPING has started.**
The full corpus (748 GDPR + 1,071 AI Act paragraphs) is extracted under
pipeline state `derived_actors`; the result and its QA live in
`extraction_acceptance_summary.md`. Mapping step 1 (the deterministic dry-run)
is done and the subject/modality mappings are final — full record in
`mapping_stage_summary.md` §9, status at the end of this doc. This checklist
is now a forward list for the MAPPING and
VERIFICATION stages only (§1.1, §1.2, §2, §3.1, §4.1); the extraction-stage
items (§3.2, §3.3, §3.4, §4.2) are settled and kept below for the record.

**Pipeline is NOT "frozen as evaluated" (the original plan below was superseded).**
The freeze plan was broken deliberately: the acceptance samples (test1, test2)
showed the evaluated subject guard destroying institutional-actor subjects, so
it was rewritten (`frozen` → `actorkeep` → `derived_actors`). Extraction quality
is certified on the gold sets (dev/holdout F1 ≈ 0.929; test2 F1 0.935) — see
`extraction_acceptance_summary.md` for the full pipeline-state and file map.

This doc originally listed everything tuned, deferred, or decided on the
eval-set-sized graph that must be revisited before or during scale-up.

**Standing rule:** any pipeline change (extraction, mapping, verification) re-runs the
frozen gold evaluations (`score_extraction.py`, `score_verification.py`, mapping
scoring) before numbers are quoted anywhere. The gold sets are frozen; the numbers are
a function of pipeline version. Old numbers are superseded, not falsified — every
scoring step is scripted, so regeneration is cheap. The only manual cost per change is
reviewing the delta rows it surfaces.

Tags:
- **BUILD-FIRST** — build before evaluation numbers are finalised, or the reported
  numbers describe a different pipeline than the deliverable.
- **RE-DERIVE** — data-derived parameter; recomputes at corpus by design, but needs a
  re-validation pass there, not blind reuse.
- **REVIEW-LOAD** — fails safe (flags for HITL), but flag volume at 1,819 paragraphs
  is a review budget that must be planned, not discovered.
- **EXPAND** — a deliverable currently existing as a stub or descoped placeholder.

---

## 1. BUILD-FIRST

### 1.1 Defined-term tag pass (mapping) — BUILT + PROVEN 2026-07-05; canonical 50-set application SKIPPED (documented limitation)
Status: implemented in `build_content_candidates.py` (tags read from the
records' carried `paragraph_text`), regression-tested (`test_tag_pass.py`),
and proven full-chain with zero human edits (fresh build → adjudicator →
`map_content` join maps the pt_e condition to `eu-gdpr:StorageLimitationPrinciple`
and the Art 12 object to `vair:LoggingMeasure` automatically; the noisy 89(1)
promotion is correctly rejected to literal). Decision (Yoseph, 2026-07-05): do
NOT regenerate the canonical 50-set maps — the material delta is enrichment
only, all superseded at corpus scale-up. **Limitation to report:** the
published mapping-stage numbers predate the tag pass; the tag pass is reported
separately as a validated improvement with its measured 3-row delta on the
eval set. It applies from corpus scale-up onward.
`build_content_candidates.py` currently defaults no-lexical-hit conditions/predicates
to `literal`, so they never reach the adjudicator — and the adjudicator is propose-only
from the top-20 embedding candidates, so it cannot find a concept the candidate list
lacks. Documented near-miss: GDPR Art 5(1)(e)'s condition is the storage-limitation
principle, but `eu-gdpr:StorageLimitationPrinciple` had no lexical hit, was absent from
the top-20 embeddings, and defaulted to `literal`; only manual review caught it — and
the conflict detector's b-side anchor depended on it.

Fix: scan each source paragraph for the legislator's own quoted defined-term tags
(`(‘storage limitation’)`, `(‘accuracy’)`, Art 4 / Art 3 definitions…); when the tag's
lemmatised tokens are contained in a routed vocab label (relaxed direction: tag ⊆
label), inject that concept as a high-priority `tag` candidate on the paragraph's rows
and promote `literal → review`. High precision (the tag is the regulation's own name
for the concept); bounded review inflation (tags are sparse).

After building: re-run the builder on the same dev/holdout dirs, diff the worksheet,
adjudicate/review the changed rows, re-apply, re-score. Validation case: it must
surface `eu-gdpr:StorageLimitationPrinciple` on the conflict-pair pt_e rows unaided.

Residual (report honestly): the tag pass only covers legislator-named concepts.
Mapping recall bounds conflict-detection recall — the "human review is structural,
not optional" finding extended to corpus scale.

### 1.2 Operand-vs-mention detector FP class (verification) — DECIDED 2026-07-05: not built; documented limitation
The remaining verification FP class: a concept IRI (e.g. `dpv:PersonalData`) present
on a statement only because a noun phrase mentions it ("personal data *breach*"),
not because the act operates on it. 4 worksheet rows; tension precision 0.69.
`verification_stage_summary.md` records the fix (object head-noun analysis) as
"deliberately not built — four rows do not justify new machinery."

Decision: option (b) — keep as a *reported* schema limitation; the numbers are
frozen as measured (tension P 0.71 / R 0.91, with the FP class named and its
mechanism explained: the schema does not encode whether a concept is the act's
operand or embedded in a noun phrase). The candidate fix (object head-noun
analysis) is deliberately not built — the mapping stage's abandoned head-noun
gate is the cautionary precedent, and four eval-set rows do not justify the
machinery. **Revisit at corpus scale-up:** measure the FP rate on a corpus
sample first (the breach-notification family multiplies the class); if the
review queue it generates is unmanageable, build the fix then and re-score
against the same worksheet gold (no LLM cost).

### 1.3 Real Art 12 AIA / Art 5(1)(e) GDPR conflict pair — DONE 2026-07-05
Extracted, mapped (content mappings human-reviewed), loaded, verified:
`logging_vs_storage_limitation` fires on real statements
(`aiact:art_12/par_1#s1` × `gdpr:art_5/par_1/pt_e#s2`, synthetic:false);
synthetic pair retired; conflict pair `:Verified:HumanReviewed` after legal
sign-off via the worksheet-driven disposition step in `verify_statements.py`.

### 1.4 5-run verification stability replay — DECIDED 2026-07-05: not run; documented limitation
**Limitation to report:** the verification detector is scored on the canonical
run (run4) only; unlike extraction (which has cross-run agreement, cycle
consistency, and 5-run means), the verification stage carries no cross-run
stability claim. Known consequence of run variance already observed: run4's
`art_12/par_1#s1` lacks the condition present in run1 (the documented
condition-wobble class). The replay remains cheap (no LLM: load runN → verify →
compare verdicts) if a reviewer asks for it.

## 2. RE-DERIVE at corpus (by design, but must be re-validated)

### 2.1 Generic-concept tier (discriminative-overlap gate)
Defined as concepts on ≥10% of deontic candidates — measured on 51 statements
(currently {PersonalData, Processing, GDPRRightsImpact, ProcessingContext,
MakeAvailable}). Recomputes at corpus; membership will change; detector behaviour
with it. Re-validate on a labelled sample before trusting corpus verdicts.

### 2.2 Duplicate-check thresholds
Pred-set equality + condition Jaccard ≥ 0.5, tuned to zero false firings on 51
statements. Unknown behaviour at corpus scale; sample-check firings there.

## 3. REVIEW-LOAD (fails safe, but budget the human time)

### 3.1 Curated mapping tables — dry-run DONE 2026-07-12
Subject lexicon, `predicate_synonyms.json`, `object_aliases.json`, IDF floor — all
built against eval-set values. Every corpus value they don't cover defaults to
`literal`/`unmatched` (the §1.1 failure mode) or lands in the adjudication/review
queue. Expect at least one full adjudication + manual-review round at corpus size;
estimate row counts early by running the builder over the corpus extractions and
counting dispositions before committing to review.

**Status:** the row-count estimate is done and the subject lexicon is extended
and final (3 → 16 roles; subject coverage 44% → 68%, residual is genuine
vocabulary gaps / composites / non-actor subjects). Full numbers in
`mapping_stage_summary.md` §9. Review queue: 1,124 rows, awaiting the
review-budget decision.

### 3.2 Closed lists in extraction guards
Enumeration-gate condition-introducer whitelist (unknown conditional enumerations →
flag, not gate), `_ACTION_NOMINALS`, `_DEONTIC_OPERATORS`. Under-coverage at corpus
= more `needs_review` flags. Measure flag rates on a corpus sample first; extend the
whitelists from observed cases (with regression assertions in the style of
`test_subject_guard.py` / `test_predicate_guards.py`), not speculation.

### 3.3 Art 32 extras decision (chapeau + measure sub-points) — DECIDED 2026-07-10: keep current behaviour
"Shall include the following: (a)…(d)" measure sub-points stay independent
OBLIGATION statements. Adopted as the extraction *convention*, not an error:
each measure is individually checkable, and the pipeline stays frozen as
evaluated. The 4 eval-set extras are re-described in the write-up as a
convention difference between gold and pipeline, not a precision failure.
No gold added; no code change.

### 3.4 Definition-prefix flakiness (G10/H01/H02 class) — DECIDED 2026-07-10: skipped; documented limitation
The `'X' means` prefix intermittently included in `definition.value`. The
deterministic prefix-strip post-pass is deliberately NOT built: the pipeline is
frozen exactly as evaluated, so the frozen gold numbers remain the deliverable's
numbers with no re-run owed. Consequence to report honestly: GDPR Art 4 has 26
definitions, AI Act Art 3 has 68 — the class multiplies ~30× at corpus and
Definitional is already the weakest category (holdout 0.500 [0.000,1.000]).
The limitations section names this class and its scale explicitly.

## 4. EXPAND (stubs of deliverables)

### 4.1 CONFLICT_PATTERNS table — grounding requirement added 2026-07-10
One entry (`logging_vs_storage_limitation`). The report promises a "curated set of
known conflict patterns", and the MigrainePredict scenario needs at minimum:
- Art 25 GDPR (data protection by design) ↔ Art 10 AI Act (data governance) —
  parallel-obligation composition, not conflict;
- Art 9 GDPR (special-category prohibition) ↔ Art 10(5) AI Act (special-category
  processing *permitted* for bias detection) — a genuine cross-regulation tension;
- Art 17 GDPR (erasure) ↔ Art 12 AI Act (log retention) — erasure-vs-retention variant.
Curate the pattern list alongside corpus extraction; each pattern needs its anchor
concepts present in the mapped graph (see §1.1 — anchor mappability is the
prerequisite).

**Grounding rule (decided 2026-07-10):** every pattern table entry carries a
citation to literature identifying the tension — no pattern is presented as
independently derived. Where the source supports the general tension but not the
exact article pair, the pattern is framed as an *operationalisation* of the cited
tension (the anchor-concept formulation is the engineering contribution). A
pattern with no source is dropped or explicitly labelled researcher-constructed.

Candidate sources located 2026-07-10 (search-verified titles/venues; READ AND
CONFIRM content before citing — snippet-level evidence only for the starred ones):
- Art 9 ↔ Art 10(5) (strongest, peer-reviewed, pair-specific):
  M. van Bekkum & F. Zuiderveen Borgesius, "Using sensitive data to prevent
  discrimination by artificial intelligence: Does the GDPR need a new exception?",
  Computer Law & Security Review (2023); and M. van Bekkum, "Using sensitive data
  to debias AI systems: Article 10(5) of the EU AI Act", Computer Law & Security
  Review (2025).
- Art 12 ↔ Art 5(1)(e) (the flagship pattern — needs grounding most urgently)*:
  "AI data governance – overlaps between the AI Act and the GDPR", Law, Innovation
  and Technology (2026), doi:10.1080/17579961.2026.2633677; EPRS study "Interplay
  between the AI Act and the EU digital legislative framework" (2025); EDPB-EDPS
  Joint Opinion 5/2021 on the AI Act proposal (authoritative on GDPR-consistency
  concerns generally).
- Art 17 ↔ Art 12 (general tension only; AI-Act-specific pair is practitioner-only
  so far): E. Fosch-Villaronga, P. Kieseberg & T. Li, "Humans forget, machines
  remember: Artificial intelligence and the Right to Be Forgotten", Computer Law &
  Security Review (2018).
- Art 25 ↔ Art 10 (parallel obligations)*: the Law, Innovation and Technology
  overlaps article (Art 10 AIA focus); "Impact assessment requirements in the GDPR
  vs the AI Act: Overlaps, divergence, and implications" (2026) for the
  DPIA-vs-FRIA analogue.

### 4.2 Temporal / screen_dependent handling — DECIDED 2026-07-10: descoping extended to the corpus KG
H11/H14 removed; `screen_dependent` dormant in the harness. Entry-into-force and
application-date provisions (Art 99 AIA etc.) extract as whatever the frozen
pipeline emits; the corpus KG does not model applicability-in-time. Documented
limitation in the write-up; the dormant path is NOT revived.

---

## Sequencing (updated 2026-07-05 — §1 fully dispositioned)

1. ~~§1.3 conflict pair~~ DONE. ~~§1.1 tag pass~~ BUILT + PROVEN (applies from
   scale-up; canonical 50-set left as measured). §1.2 and §1.4 carried as
   documented limitations (details in their sections).
2. Phase 2 GraphRAG proceeds against the current 54-statement graph (self-
   contained: statements, mappings, provenance, provision text; reproducible
   from repo files alone).
3. At scale-up: §2 re-derivations, §3 budgeted review rounds (§1.2's FP-rate
   measurement joins §3.1's sample counts), §4 expansions — then re-run
   load → verify → resolve at corpus size, reviewing only unresolved flags.

## Scale-up strategy (confirmed 2026-07-10; extraction executed 2026-07-12)

- **Scope: FULL corpus** (748 GDPR + 1,071 AI Act postscreened paragraphs). DONE.
  A hand-picked "relevant provisions" subset was considered and rejected:
  scope-selection is itself an ungrounded legal mapping, and a partial corpus
  makes INSUFFICIENT verdicts ambiguous (law silent vs not ingested), breaking
  the no-inference-from-silence story.
- **Pipeline: `derived_actors` (NOT frozen — plan superseded).** The original
  "freeze as evaluated" plan was abandoned when test1/test2 exposed the subject
  guard destroying institutional-actor subjects; the guard was rewritten and
  re-certified on test2 (F1 0.935). §3.3 convention kept, §3.4 skipped, §4.2
  descoping extended still hold. Details + file map in
  `extraction_acceptance_summary.md`.
- **Corpus extraction QA:** DONE — single extraction run, gold-free checks
  (`span_grounding.py` ~98.5% grounded, `schema_validity.py` 100% valid /
  97.7% content-complete) + acceptance sampling (test1 diagnostic, test2
  reportable). All in `extraction_acceptance_summary.md`.
- **Mapping (IN PROGRESS):** the dry-run count is DONE (2026-07-12) and the
  subject/modality mappings are final as-is — full result in
  `mapping_stage_summary.md` §9. Next gate: the review-budget decision on the
  1,124 content-slot review rows, then adjudication → human confirm →
  `map_content`.
- **Query-time eval:** re-run the EXISTING gold 50 only (no new scenario
  queries) three-way — pipeline-Gemini, pipeline-Mistral, vector baseline —
  against the corpus graph. Controlled before/after; the Q25/S3 coverage-gap
  closures are the predicted headline.
- **Standing rule:** any further pipeline change re-runs the gold evals before
  numbers are quoted (now a LIVE run, not a replay — replays do not exercise the
  live subject-inference path; see `extraction_acceptance_summary.md`).

## Corpus mapping — CLOSED (2026-07-12)

The corpus mapping stage is complete. **The full record lives in
`mapping_stage_summary.md` §9** (the mapping stage's own document); it is not
duplicated here.

Headline: subjects 1,192/1,744 mapped (68%, lexicon final); modality
1,627/1,627; all 1,124 content review rows adjudicated (proposals only);
human review at corpus scale = a 9-row confirmed sample + the 40 carried
eval-scale decisions; all remaining `llm_suggested_*` proposals enter the
graph carrying provenance status (Yoseph's decision). Apply-back done:
`data/{gdpr,aiact}.content_mapped.jsonl` (2,536 records), both flagship
conflict anchors confirmed mappable.

**Next stage: load → verify at corpus scale** — §2 re-derivations
(generic-concept tier, duplicate thresholds), §1.2 operand-vs-mention FP-rate
sample, §4.1 CONFLICT_PATTERNS expansion.
