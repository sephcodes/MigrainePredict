# Scale-up readiness checklist

Full-corpus scale-up (748 GDPR + 1,071 AI Act postscreened paragraphs) is the immediate
next deliverable after the 50-statement evaluation pipeline — the deliverable KG must
cover the MigrainePredict scenario, which the current 51-statement graph does not.
This doc lists everything tuned, deferred, or decided on the eval-set-sized graph that
must be revisited before or during scale-up, so nothing rides along silently.

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

### 1.1 Defined-term tag pass (mapping) — agreed 2026-07-05
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

### 1.2 Operand-vs-mention detector FP class (verification) — decision needed
The remaining verification FP class: a concept IRI (e.g. `dpv:PersonalData`) present
on a statement only because a noun phrase mentions it ("personal data *breach*"),
not because the act operates on it. 4 worksheet rows; tension precision 0.69.
`verification_stage_summary.md` records the fix (object head-noun analysis) as
"deliberately not built — four rows do not justify new machinery."

That justification is count-based on the 51-statement graph and does not survive
corpus scale: the breach-notification statement family (Art 33/34-shaped) recurs
throughout both regulations, so the class multiplies. Decide now, with corpus in view:
(a) build pre-finalisation and re-score against the same 89-pair worksheet gold
(no LLM cost), or (b) keep as a *reported* schema limitation and freeze the number.
The mapping stage's abandoned head-noun gate is the cautionary precedent for (a).

### 1.3 Real Art 12 AIA / Art 5(1)(e) GDPR conflict pair — in progress 2026-07-05
Extracted, subject/modality-mapped, content mappings human-reviewed
(`mapping/content_map_reviewed_2_conflict_pair.json`). Remaining: `map_content` →
load (`--input CONFLICT:…`) → `verify_statements.py --no-synthetic` → confirm
`logging_vs_storage_limitation` fires on real statements; retire the `:Synthetic` pair.

### 1.4 5-run verification stability replay — cheap, closes a gap in the story
Verification was only run and scored on run4. Every other stage has a cross-run
stability claim; this one doesn't. No LLM cost: load runN → verify → compare verdict
sets across the 5 runs.

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

### 3.1 Curated mapping tables
Subject lexicon, `predicate_synonyms.json`, `object_aliases.json`, IDF floor — all
built against eval-set values. Every corpus value they don't cover defaults to
`literal`/`unmatched` (the §1.1 failure mode) or lands in the adjudication/review
queue. Expect at least one full adjudication + manual-review round at corpus size;
estimate row counts early by running the builder over the corpus extractions and
counting dispositions before committing to review.

### 3.2 Closed lists in extraction guards
Enumeration-gate condition-introducer whitelist (unknown conditional enumerations →
flag, not gate), `_ACTION_NOMINALS`, `_DEONTIC_OPERATORS`. Under-coverage at corpus
= more `needs_review` flags. Measure flag rates on a corpus sample first; extend the
whitelists from observed cases (with `test_enumeration_gate.py`-style regression
assertions), not speculation.

### 3.3 Art 32 extras decision (chapeau + measure sub-points)
Still open: add gold / accept as extras / model measures as object-content of the
chapeau. The "shall include the following: (a)…(d)" paragraph shape recurs constantly
at corpus, so whichever convention is chosen affects extraction precision everywhere.
Decide before corpus extraction, not after.

### 3.4 Definition-prefix flakiness (G10/H01/H02 class)
The `'X' means` prefix intermittently included in `definition.value`. Parked as
"open next candidate" at eval scale, where it costs ~2 HARD flags. GDPR Art 4 has 26
definitions, AI Act Art 3 has 68 — the class multiplies ~30× at corpus and
Definitional is already the weakest category (holdout 0.500 [0.000,1.000]).
A deterministic prefix-strip post-pass is the obvious candidate (working-style:
post-pass, not prompt edit).

## 4. EXPAND (stubs of deliverables)

### 4.1 CONFLICT_PATTERNS table
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

### 4.2 Temporal / screen_dependent handling (descoped by decision)
H11/H14 removed; `screen_dependent` dormant in the harness. Entry-into-force and
application-date provisions (Art 99 AIA etc.) are present in the postscreened corpus
and currently have no handling. Either extend the descoping decision explicitly to
the corpus KG (documented limitation) or revive the dormant path. Explicit decision
either way — silence is the failure mode.

---

## Sequencing

1. Finish §1.3 (conflict pair — in flight).
2. Build §1.1 (tag pass); decide §1.2; run §1.4. Re-run frozen gold evals; Yoseph
   reviews the deltas; regenerated numbers become the reported numbers.
3. Phase 2 GraphRAG proceeds against the then-current graph (its own metrics are
   query-time and unaffected by mapping-stage regeneration, but graph rebuilds must
   precede gold-answer authoring for the query eval set).
4. At scale-up: §2 re-derivations, §3 budgeted review rounds, §4 expansions —
   then re-run load → verify → resolve at corpus size, reviewing only unresolved flags.
