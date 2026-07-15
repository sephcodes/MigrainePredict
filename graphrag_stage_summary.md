# GraphRAG Query Layer — Build Notes (Phase 2, in progress)

*Phase 2 of the MigrainePredict pipeline: answering natural-language compliance
questions against the knowledge graph built in Phase 1. Companion to the
Phase 1 retrospectives (mapping_stage_summary.md, verification_stage_summary.md);
this one is a running build log, not a retrospective — the stage is not finished.*

---

## 1. What this stage does

A user asks a question in plain English ("Can MigrainePredict retain
operational logs for 90 days?"). The system finds the relevant statements in
the knowledge graph, pulls in anything connected to them (exceptions,
cross-references, the known GDPR-vs-AI-Act conflict), and has the LLM write a
verdict — one of COMPLIANT / NON_COMPLIANT / INSUFFICIENT / NOT_APPLICABLE —
with an explanation that cites the exact statements it used. Everything runs
against the 54 verified statements only; flagged or unreviewed material is
invisible to queries.

Implementation: one plain Python script, `graphrag_query.py`, with explicit
function stages (a LangGraph state machine was considered and rejected as
overkill — the report text will be updated to match).

## 2. How a question flows through it

1. **Understand the question.** An LLM call classifies it: which of the 43
   covered provisions is it about, and what concrete facts does the question
   state. The LLM is shown the list of covered provisions and may only pick
   from it — it cannot invent articles.
2. **Find seed statements (three routes, tried in order).**
   - If the question maps to covered provisions: a fixed, deterministic graph
     query fetches their statements. No LLM involved. This is the expected
     common case.
   - Otherwise: the LLM writes a read-only graph query itself. The query is
     dry-run checked first; if it has errors, the error message is fed back
     and the LLM retries, up to 3 attempts (all attempts logged — this gives
     the self-correction statistics the report promises to compare against
     Echenim & Joshi's 18%→0%).
   - If that also fails: fall back to embedding similarity — find the 3
     covered provisions whose text is closest to the question and use their
     statements. Deterministic safety net.
3. **Expand.** From the seeds, follow one hop of the graph's relationship
   edges (cross-references, exceptions, conflicts) so that, e.g., a question
   touching the AI Act logging duty automatically also retrieves the GDPR
   storage-limitation duty it conflicts with.
4. **Add text snippets.** Embedding search over the text of the 43 covered
   provisions only (cached index in `data/graphrag/`). Deliberate scope
   limit: the answer can never quote regulation text the graph doesn't know.
5. **Write the verdict.** One structured LLM call gets the statements (with
   their original source sentences), the relationship edges, and the
   snippets. Rules in the prompt: never infer a violation from something not
   being mentioned (missing scenario detail → INSUFFICIENT; nothing relevant
   retrieved → NOT_APPLICABLE), and only cite IDs that appear in the context.
6. **Route.** INSUFFICIENT verdicts are appended to a review queue file for a
   human to look at. Every step of every query is audit-logged
   (`data/graphrag/audit_log.jsonl`); full results go to
   `data/graphrag/results.jsonl`.

## 3. Decisions taken (confirmed 2026-07-05/06)

- **Hybrid query generation** — deterministic templates first, LLM-written
  queries only as fallback, embedding fallback last. Keeps the common path
  reproducible while still exercising the self-correction loop the report
  describes.
- **Advisory mode only** for now ("does this behaviour comply?"). The
  policy-audit mode ("does this document cover the rules?") stays designed-in
  but unbuilt until Skein's vendor policy arrives.
- **Snippet index covers the 43 covered provisions only** — the 21 provisions
  that exist in the graph purely because something cites them have no text
  and are not indexed. The later vector-only baseline will use this same
  index, so the comparison isolates what the graph structure adds.
- **Gold query set is sized after smoke testing** (pencil: ~30 questions plus
  paraphrase variants of ~10).

## 4. Status

- **Skeleton built and structurally self-checked** (`--check`, no LLM):
  templates are valid, the conflict pair is retrieved with its edges, the
  snippet index's top hit for a logging question is the AI Act logging
  provision.
- **First live query run** — the report's own worked example (§5.5), "Can
  MigrainePredict retain operational logs for 90 days?": verdict
  **INSUFFICIENT**, which is the outcome the report itself predicts for this
  question, reached via the intended path — deterministic template, correct
  provisions, all three conflict-pair statements and all three relationship
  edges retrieved, both citations real, result auto-routed to the review
  queue. Findings from this run are consolidated in §6 (F1, F2).

## 5. Smoke queries (run 2026-07-06, five in total)

Inputs in `data/graphrag/smoke_queries.jsonl`; full records in
`data/graphrag/results.jsonl`.

| id | question topic | verdict | seed path | assessment |
|----|----------------|---------|-----------|------------|
| (pre) | retain logs 90 days | INSUFFICIENT | template | matches the report's own worked example; see §4 |
| S1 | health data + explicit consent | COMPLIANT | template | textbook chain: health-data definition → general ban → consent exception → consent definition; all citations real |
| S2 | erasure request vs research study | INSUFFICIENT | template | strongest answer: used both erasure statements, the research exceptions and the storage-limitation pair via their edges, and correctly declined to drag in the logging conflict |
| S3 | is it high-risk under the AI Act | INSUFFICIENT | template | the weak one — see below |
| S4 | cookies on the marketing website | INSUFFICIENT | template | intended as an off-topic probe, but cookies genuinely involve personal data, so finding GDPR targets was fair; answer reasonable but sprawling (8 citations) |
| S5 | annual fire-extinguisher inspection | NOT_APPLICABLE | vector fallback | truly off-topic probe: no targets found → LLM query route → vector fallback → correct "nothing governs this" |

INSUFFICIENT verdicts (90-day, S2, S3, S4) were auto-routed to
`data/graphrag/review_queue.jsonl` as designed.

## 6. Findings from the first live runs (90-day query + smoke set)

Consolidated list, reviewed by Yoseph 2026-07-06. Each is a documented
observation for the evaluation chapter, not a bug ticket — none blocks
progress, and several are exactly what the planned metrics are built to
measure.

- **F1 — Hallucinated resolution mechanism (90-day query).** One sentence of
  the explanation stated that such conflicts are "often resolved by
  anonymising or pseudonymising log data". That mechanism is nowhere in the
  retrieved context — the graph has no pseudonymisation exception — so the
  model imported it from general knowledge. The citation list stayed clean
  (the hallucinated mechanism cited nothing), and the verdict was unaffected,
  but this is a faithfulness violation in the explanation text: precisely
  what the faithfulness metric measures, and the first live evidence that
  explanation prose and citation discipline can diverge.
- **F2 — Over-reading a capability duty as a retention duty (90-day query).**
  The AI Act statement says systems must "technically allow" automatic
  recording of events; the model read this as implying logs must be *kept*
  for the system's lifetime. Hedged, and harmless here, but an interpretive
  step beyond the statement text.
- **F3 — KG coverage gap surfaced honestly, but not named (S3).** The graph's
  Article 6(1) statement is chapeau-only: "high-risk where both of the
  following conditions are fulfilled" — the two conditions themselves live in
  provisions the graph knows only as reference targets, with no text
  (art_6/par_1/pt_a and pt_b are referenced-only). So no system could have
  decided the high-risk question from this graph: INSUFFICIENT was the only
  reachable correct verdict. Two genuine defects remain: (a) the model
  retrieved the Article 6(1) statement but never used or cited it — the ideal
  answer says "a classification route exists whose conditions I cannot see";
  (b) its missing-information request (an Annex III listing for medical
  devices) points at the wrong gap. The refusal to invent Annex III content
  is the no-inference-from-silence rule holding. Failure-mode names for the
  evaluation: *coverage gap* (KG-side) and *retrieved-but-unused evidence*
  (synthesis-side). The coverage gap itself closes at corpus scale-up.
- **F4 — The self-correction loop works and has real data (S5).** The LLM's
  first two generated queries failed at execution (it treated the graph's
  list-valued text fields as strings); each error was fed back and attempt 3
  was valid, correctly returning zero rows. Nuance worth reporting: the
  EXPLAIN dry-run catches syntax errors only — these were runtime type
  errors, which the loop also catches because execution errors feed back the
  same way.
- **F5 — INSUFFICIENT-heavy distribution (3 of 5 smoke, 4 of 6 overall).**
  Partly by design — no-inference-from-silence pushes borderline cases there
  — but over-use of INSUFFICIENT is itself a failure mode the evaluation must
  measure. The gold set needs scenarios stated fully enough that COMPLIANT /
  NON_COMPLIANT is sometimes the right answer.
- **F6 — NOT_APPLICABLE probes must contain no data-processing at all (S4).**
  Anything touching personal data legitimately activates GDPR provisions, so
  the cookies probe could never test NOT_APPLICABLE. The gold set needs a
  crisp written rule for the INSUFFICIENT vs NOT_APPLICABLE boundary.
- **F7 — Classification-style questions fit the labels awkwardly (S3).**
  "Is this system high-risk?" is not a compliance question; the four labels
  strain to hold it. Gold questions should be phrased as activity/obligation
  questions where possible.

## 7. Review queue: proposed correct answers (claude_proposed, pending review)

For the four INSUFFICIENT results routed to the queue. In all four the
system's verdict LABEL matches the proposed gold label; the differences are
in the explanations — which is why the review structure (below) judges
verdict and explanation separately.

- **90-day logs → INSUFFICIENT (system correct).** Gold explanation: the AI
  Act logging duty and the GDPR storage-limitation duty both apply and are in
  recorded conflict; the graph's only route to longer storage is the
  research/statistics exception with safeguards. Whether 90 days complies
  depends on facts the question doesn't give: whether the logs contain
  personal data, and what retention their purpose actually needs.
  System's explanation acceptable **minus the F1 sentence**.
- **S2 erasure vs research → INSUFFICIENT (system correct, explanation
  essentially gold).** Erasure duty applies; the research exceptions could
  lift it, but only under conditions (necessity, Article 89(1) safeguards)
  the scenario doesn't establish. Confirm as-is.
- **S3 high-risk → INSUFFICIENT (label correct, explanation deficient).**
  Gold explanation: the graph contains a classification route (Article 6(1))
  whose two qualifying conditions are referenced but not present in the
  graph, and its only covered Annex III area is Biometrics — so the
  question cannot be decided from this graph. Missing information: the
  content of the Article 6(1) conditions (and any further Annex III areas),
  i.e. a stated coverage gap — not specifically an Annex III medical-device
  listing.
- **S4 cookies → INSUFFICIENT (system correct).** Visitor tracking via
  cookies can identify people, so the data rules apply; whether it complies
  turns on consent/transparency facts the question doesn't state. Confirm;
  the probe-design lesson (F6) is about the gold set, not this answer.

## 8. Next

1. ~~Query-review worksheet~~ BUILT (`make_query_review_worksheet.py` →
   `data/graphrag/query_review.json`, mirroring the verification-worksheet
   pattern): one row per queued query; system output read-only; human fields
   `human_verdict` (same four labels), `human_explanation` (the gold trace),
   `explanation_assessment` (correct/partly/wrong — verdict and explanation
   are judged separately, the S3 lesson), `notes`, `label_source`. Rows with
   judgments survive regeneration (verified). The §7 proposals are seeded as
   `label_source: claude_proposed` — Yoseph reviews, edits, and flips to
   `human` on adoption. Reviewed rows double as the first gold answers for
   the evaluation harness.
2. Yoseph reviews the four seeded worksheet rows.
3. ~~Gold query set~~ DRAFTED (`data/graphrag/gold_queries.json`,
   `label_source: claude_proposed` throughout, pending review): 30 base
   questions — 9 COMPLIANT, 10 NON_COMPLIANT (recall-weighted, matching the
   F2 emphasis), 6 INSUFFICIENT, 5 NOT_APPLICABLE. Every question grounded in
   the 54 verified statements (all citations mechanically checked against
   the graph); structure coverage: 7 exception questions (Art 9, Art 17,
   Art 5(1)(e) both ways), 2 conflict questions (Q3 resolved / Q20
   unresolved — the flagship pair), 1 deliberate coverage-gap probe (Q25,
   records exemption — the S3 lesson as a test case), 3 zero-data
   NOT_APPLICABLE probes + 2 grounded ones decided FROM the definitions
   (anonymous statistics; non-AI FAQ page). The file carries the written
   labelling rules (INSUFFICIENT vs NOT_APPLICABLE boundary; NON_COMPLIANT
   gold must state that exceptions are ruled out; activity phrasing). Ten
   rows are flagged `paraphrase_candidate`; the 2-per-question paraphrase
   variants are generated only after Yoseph adopts the base questions, so
   his edits don't orphan them.
4. ~~Base-set review~~ DONE — Yoseph adopted the 30 questions 2026-07-06
   (edits applied, label_source stripped per convention).
5. ~~Paraphrases~~ GENERATED — 20 variants (2 each for the 10 flagged
   questions) in the file's separate `paraphrases` array, wording-only
   changes, gold stored once on the base row and resolved via
   `paraphrase_of`. Pending Yoseph's quick read ("does it still ask the
   same question with the same facts?"). 50 prompts total.
6. ~~Full 50-prompt run~~ DONE 2026-07-07 (`data/graphrag/gold_run1.results.jsonl`;
   batch input `gold_run_batch.jsonl`). Headline numbers (informal grading;
   the harness will formalise):
   - **Completeness 50/50**, no failed or empty responses.
   - **Verdict agreement 46/50 (92%)** — base 26/30, paraphrases 20/20.
   - **Paraphrase stability 10/10 trios internally consistent** (every
     reworded version got the same verdict as its base — first live
     paraphrase-sensitivity evidence, and it is clean).
   - **NON_COMPLIANT recall 16/16, NOT_APPLICABLE 9/9** (including both
     grounded NA trios — anonymous statistics and the non-AI FAQ page).
   - Seed paths: 47 template, 2 vector fallback, 1 LLM-Cypher; the
     self-correction loop ran on 3 queries (8 attempts, 7 errors fed back).
   - **The four misses form two classes, no polar (C↔NC) errors:**
     - *Over-caution / question-scope creep (Q08, Q09: gold COMPLIANT →
       system INSUFFICIENT).* The model agreed the asked-about rules were
       satisfied, then demanded facts about a DIFFERENT layer (the Article 9
       legal basis for the underlying health data) that the question did not
       ask about. Defensible caution for a compliance assistant; wrong
       against a question-scoped gold. Yoseph to adjudicate: tighten the
       synthesis prompt to answer the question asked, or accept and name the
       behaviour.
     - *Over-decisiveness (Q23, Q25: gold INSUFFICIENT → system
       NON_COMPLIANT).* Q23 judged two-year-old bundled sign-up consent as
       failing the consent definition instead of asking for its quality — a
       genuine boundary case ('general sign-up flow' arguably IS a stated
       fact against specificity; gold could defensibly flip). Q25 is the
       coverage-gap probe doing its job: the model correctly found the
       records exemption unavailable, then asserted the underlying
       record-keeping duty — whose content is NOT in the graph — from
       general knowledge. The mirror image of S3: instead of naming the gap
       it filled the gap. Flagship failure-mode evidence for the
       no-inference-from-silence limits discussion.
7. ~~Adjudication of the four misses~~ DONE (Yoseph, 2026-07-07). Gold stays
   as authored for all four; each miss is recorded, not patched:
   - **Q08, Q09 → reported LIMITATION** ("question-scope creep /
     over-caution"): the system may withhold COMPLIANT by demanding facts
     about a regulatory layer the question did not ask about.
   - **Q23 → reported AMBIGUITY**: "consent given inside an old app
     version's general sign-up flow" can defensibly be read either as a
     stated fact against consent specificity (→ NON_COMPLIANT) or as
     leaving consent quality unknown (→ INSUFFICIENT). A genuine
     INSUFFICIENT/NON_COMPLIANT boundary case, reported as such.
   - **Q25 → reported LIMITATION** (with a precise characterisation that
     matters for the write-up): on close reading, the explanation is almost
     entirely grounded — the exemption's carve-out conditions come from the
     retrieved provision text and are applied correctly. The failure is
     exactly ONE inferential step: from "the exemption from the
     record-keeping duties does not apply" to "therefore the duty applies
     and skipping it is a violation" — asserting a duty whose content and
     addressees (Article 30(1)-(2)) are not in the graph. Notably, that
     pivotal step is the only claim in the answer carrying NO citation, so
     the citation discipline makes the leap mechanically visible in the
     trace. Reported as a limitation rather than fixed: the planned
     faithfulness metric quantifies this class; the advisory-only + review
     queue design is the operational mitigation; this instance disappears
     at corpus scale-up (Article 30(1)-(2) enters the graph) though the
     class remains for whatever stays uncovered; and a runtime
     faithfulness gate (judge each explanation claim against the retrieved
     context, route low-support answers to review) is named as future
     work, not built.
8. ~~Evaluation harness + vector-only baseline~~ BUILT AND RUN 2026-07-07.
   `score_query.py` = the harness (offline: adherence, per-label P/R/F1/F2,
   macro-F1, confusion, citation recall/precision, paraphrase sensitivity,
   loop stats, miss list; `--live`: RAGAS-style faithfulness + answer
   relevance, formulas implemented directly with a Gemini judge + bge
   cosine). Baseline = `graphrag_query.py --baseline`: plain vector RAG over
   the SAME covered-only snippet index, same verdict rules, no graph/intent.
   Metrics JSONs: `gold_run1.metrics.json`, `baseline_run1.metrics.json`.

   **System vs baseline (offline, 50 prompts each):**

   | metric | GraphRAG | vector-only RAG |
   |---|---|---|
   | adherence rate | **0.920** | 0.820 |
   | macro-F1 | **0.917** | 0.816 |
   | NON_COMPLIANT recall / F2 | **1.000 / 0.976** | 0.812 / 0.833 |
   | paraphrase: consistent trios | **10/10** (range 0.000) | 6/10 (range 0.200) |
   | citation recall vs gold statements | **0.946** | 0.000 (structural) |

   Per-label detail (P / R / F1 / F2; full raw numbers in the two
   `.metrics.json` files):

   | label | GraphRAG | vector-only RAG |
   |---|---|---|
   | COMPLIANT | 1.000 / 0.867 / 0.929 / 0.890 | 1.000 / 0.733 / 0.846 / 0.775 |
   | NON_COMPLIANT | 0.889 / 1.000 / 0.941 / 0.976 | 0.929 / 0.812 / 0.867 / 0.833 |
   | INSUFFICIENT | 0.800 / 0.800 / 0.800 / 0.800 | 0.643 / 0.900 / 0.750 / 0.833 |
   | NOT_APPLICABLE | 1.000 / 1.000 / 1.000 / 1.000 | 0.727 / 0.889 / 0.800 / 0.851 |

   **Citation precision caveat (one sentence for the write-up):** the
   pipeline's citation precision is 0.445 (236 system-cited ids vs 111 gold
   ids), which is soft BY CONSTRUCTION — gold_cited lists the minimal
   statements a correct answer must rely on, while the system cites
   everything it used from a deliberately over-complete retrieval; recall
   (0.946) is the meaningful number, and precision should be reported with
   this framing, not as a deficiency.

   **The attributable story (write-up material):** the baseline missed the
   set's clearest violation — Q10, selling health-risk scores to advertisers
   with consent explicitly absent — in all three wordings (NOT_APPLICABLE
   x2, INSUFFICIENT x1). Its own explanation NAMES Article 9 as governing,
   but vector search never surfaced the Article 9 text, and under
   no-inference-from-silence it therefore correctly refused to find a
   violation: a retrieval failure, not a reasoning failure. The GraphRAG
   intent stage maps "health data, no consent" to the Article 9 provisions
   deterministically, so all three wordings verdict NON_COMPLIANT. Same
   mechanism explains the paraphrase gap (embedding neighbourhoods shift
   with wording; grounded intent targets do not). Citation recall 0 for the
   baseline is structural — it has no statements to cite, only provision
   text — which is itself the explainability difference (FR8): baseline
   answers cannot be traced to verified statements. Baseline also
   reproduced the Q25 gap-filling leap, confirming it is a
   synthesis-level failure mode independent of retrieval route.

   Housekeeping: pipeline eval runs route their INSUFFICIENT verdicts to
   `review_queue.jsonl` like any other query (designed behaviour firing
   during evaluation), so the queue now contains gold-run entries beyond
   the four adopted worksheet rows. Regenerating the worksheet would add
   rows for them; the adopted rows survive regardless. The baseline mode
   deliberately does not route.
9. ~~Live faithfulness/relevance~~ DONE 2026-07-08, both runs 50/50, no
   failures (after two rounds of hardening: three transient-error
   signatures added to the shared retry list; per-query failure isolation
   in the live loop; and a judge refinement — the question is supplied to
   the judge and scenario restatements count as supported, so the metric
   isolates IMPORTED LEGAL CONTENT rather than penalising the answer for
   repeating the question. One methodology sentence: "faithfulness per
   Es et al., with the question supplied to the judge." First judge
   version without this scored baseline 0.492 on restatement noise.)

   | live metric | GraphRAG | vector-only RAG |
   |---|---|---|
   | faithfulness (mean per query) | 0.872 | 0.871 |
   | faithfulness on NOT_APPLICABLE | **0.944** | 0.760 |
   | faithfulness on INSUFFICIENT | 0.810 | **0.963** |
   | answer relevance | 0.817 | 0.808 |
   | claims per answer | 11.5 | 6.4 |

   Reading: the means are a wash, but the label-level split is the real
   finding. On NOT_APPLICABLE the baseline reasons about absence using
   imported knowledge (worst scores: Q29 0.29 — it lacks the personal-data
   definitions the pipeline retrieves, so its anonymity reasoning is
   ungrounded); the pipeline's grounded NA answers are faithful. On
   INSUFFICIENT the relation flips: the baseline says "cannot tell" in a
   few short, trivially-supported claims, while the pipeline's richer
   context invites longer reasoning with occasional unsupported steps.
   Pipeline answers carry ~1.8x more claims at equal claim-level support
   (~14% unsupported both) — richer explanations, same per-claim
   grounding.

   **Correction to the Q25 mitigation story (honesty note):** Q25 scored
   faithfulness 1.00 — the judge ruled "the duty applies" as ENTAILED by
   the exemption text (an exemption from duties implies the duties exist),
   so the faithfulness metric does NOT catch this failure class, contrary
   to what item 7 anticipated. The reliable detector for Q25-class leaps
   remains the citation trace: the pivotal claim is the only one carrying
   no citation. The limitations section should say faithfulness measures
   imported legal content generally, while gap-bridging entailments
   require the uncited-claim check (a mechanical scan, future work).
10. ~~Backend model-sensitivity comparison~~ DONE 2026-07-08. Same pipeline,
    same 50 prompts, same prompts/schema — only the LLM backend swapped
    (`graphrag_query.py --backend mistral`, local Ollama; Yoseph ran it).
    Results `data/graphrag/gold_run1_mistral.{results,metrics}.json`. This
    reproduces the report's own model-sensitivity theme (Chung's Llama-3-8B
    Micro-F1 drop, Mavridis' GPT-4o-vs-Llama gap) inside our pipeline.

    | metric | Gemini 2.5 Flash | Mistral (local) |
    |---|---|---|
    | adherence | **0.920** | 0.820 |
    | macro-F1 | **0.917** | 0.778 |
    | INSUFFICIENT F1 / recall | **0.800 / 0.800** | 0.429 / 0.300 |
    | NON_COMPLIANT F1 | **0.941** | 0.833 |
    | COMPLIANT F1 | **0.929** | 0.903 |
    | NOT_APPLICABLE F1 | **1.000** | 0.947 |
    | paraphrase-consistent trios | **10/10** | 8/10 |
    | citation recall / precision | 0.946 / 0.445 | 0.441 / 0.653 |
    | faithfulness (mean) | **0.872** | 0.788 |
    | answer relevance | 0.817 | **0.839** |
    | claims per answer | 11.5 | 5.3 |

    **The degradation is uneven and concentrated in one label.** Overall
    adherence falls only 0.92→0.82, but COMPLIANT / NON_COMPLIANT /
    NOT_APPLICABLE hold up (F1 >= 0.90/0.83/0.95); INSUFFICIENT recall
    collapses to 0.300 — 6 of Mistral's 9 misses are INSUFFICIENT cases it
    decided anyway (5 -> NON_COMPLIANT, 1 -> COMPLIANT). Mechanism from the
    explanations: Mistral commits exactly the error no-inference-from-
    silence forbids — Q24 "there is no information about the guarantees,
    therefore not meeting the obligation -> NON_COMPLIANT"; Q21 explicitly
    NAMES the research exception then rules NON_COMPLIANT anyway. The
    instruction is verbatim identical to the Gemini run; the weaker model
    cannot hold the abstention discipline the hardest label depends on.
    Paraphrase brittleness clusters on the same cases (Q21 flips
    NC/NC/COMPLIANT across wordings).

    **Faithfulness does NOT expose this** (same blind spot as Q25): Mistral's
    faithfulness on the INSUFFICIENT-gold cases is 0.756, comparable to its
    own average — because when it wrongly rules NON_COMPLIANT its *claims*
    are still grounded in the retrieved statements; only the silence->
    violation *inference* is wrong, and faithfulness scores grounding, not
    the correctness of legal reasoning. So Mistral's overall faithfulness
    (0.788) drops only modestly and its answer relevance is even marginally
    higher — wrong-but-confident answers read as fluent and on-topic. The
    verdict-accuracy metrics (adherence, per-label recall, F2) are what
    separate the models; the RAGAS pair does not. This is the strongest
    single argument in the eval for why verdict-level gold grading is
    necessary and RAG-quality metrics alone are insufficient for a
    compliance task. Higher Mistral citation *precision* (0.653) with far
    lower recall (0.441) = it cites fewer statements, and leaned on the
    vector fallback for 4 queries (its intent stage grounded fewer targets).

    **Framing (the pipeline-not-the-model story, and it survives scrutiny).**
    The point of this comparison is to show the ARCHITECTURE carries the
    result, not a strong proprietary model papering over a weak pipeline. The
    decomposition supports that cleanly: across the two runs the verdicts are
    near-identical EXCEPT for one failure mode. Miss decomposition —
    Gemini misses {Q08, Q09, Q23, Q25}; Mistral misses {Q03a, Q16, Q21,
    Q21a, Q21b, Q22, Q23, Q24, Q25}; the two share exactly {Q23, Q25} (the
    known INSUFFICIENT/coverage boundary cases that are pipeline-level, not
    model-level). Mistral's model-specific damage is therefore small and
    homogeneous: five of its seven extra misses are the SAME abstention
    failure (INSUFFICIENT decided confidently — Q21/Q21a/Q21b/Q22/Q24). The
    counterfactual is exact: give Mistral Gemini's abstention behaviour on
    just those five and it scores 41+5 = 46/50 = 0.920 — identical to
    Gemini's adherence. So a free, ~7B local model driven by the same
    pipeline reproduces the strong model's accuracy on COMPLIANT,
    NON_COMPLIANT and NOT_APPLICABLE, and the whole gap is one nameable,
    localised behaviour rather than diffuse weakness. (The same five errors
    also drag NON_COMPLIANT precision to 0.750, since they land as NC false
    positives — remove them and NC precision recovers too. One failure mode,
    two visible symptoms.) The dissertation reading: the KG + grounded
    intent + no-inference-from-silence design does the heavy lifting; the
    backend LLM contributes a bounded slice — verdict abstention discipline
    — that is the natural model-sensitivity locus, matching the report's
    Chung/Mavridis theme.

    **Future-work fix: a deterministic guard was HYPOTHESISED, TESTED against
    the data, and REJECTED (2026-07-08 side quest).** The idea: since the
    pipeline knows which deontic conditions attach to a matched statement, a
    pre-verdict guard could force INSUFFICIENT when the scenario does not
    supply a governing condition's operands, moving abstention off the LLM in
    the project's deterministic-guard house style. Checked the obvious
    structural signal — "retrieved subgraph contains an exception/dispensation
    attached to the governing obligation" — against the five failures and the
    correct decisive verdicts. It separates neither way: Q10 (correctly
    NON_COMPLIANT) carries 3 dispensations + an exception edge, Q01/Q05
    (correctly COMPLIANT) also do, so the signal OVER-fires on legitimately
    decisive cases; and Q24 (should abstain) has no exception edge and zero
    dispensations, so it UNDER-fires on a plain missing-fact case. What
    actually separates "abstain" from "decide" is whether the SCENARIO TEXT
    supplies or rules out the condition's operands (Q10 states "no other
    exception applies"; Q05 states the safeguards) — an irreducibly semantic
    reading a deterministic rule cannot make. Any working version needs an
    LLM abstention check, which costs the credits and reintroduces the very
    model-dependent judgment that caused the failure (a weak model would
    abstain-check as poorly as it verdicts). CONCLUSION: no cheap
    deterministic fix exists; the abstention decision is inherently semantic,
    which is itself WHY abstention is the model-sensitivity locus. This
    strengthens rather than weakens the model-sensitivity finding. Correction
    to an earlier note: the guard was described as "genuinely plausible"
    before testing; the test shows the deterministic form is not viable.
    Report the gap as a model-capability limitation (use a capable backend),
    not as a pipeline fix in waiting.
11. Expert-review worksheet (Echenim's five dimensions) for Skein.

---

## 12. Corpus scale-up query eval (2026-07-13, in progress)

Sections 1–11 describe the query layer built and evaluated against the
54-statement eval graph. This section records running the same layer against
the full corpus graph (2,271 :Verified statements, all pushed through with
`verify_statements.py --no-holdout`; see `verification_stage_summary.md` §9).

### 12.1 Mistral corpus run — garbage, and the (initially wrong) diagnosis

Yoseph ran the gold-50 on the corpus graph with the **Mistral** backend
(`data/graphrag/gold_corpus_mistral.results.jsonl`). It was clearly bad:
wrong verdicts, poor citations, retrieval dominated by **recitals** instead of
operative articles. Q01 (textbook consent, gold COMPLIANT) retrieved only
recitals and the intent stage grounded to a bare `"gdpr"` target; Q02 grounded
to a malformed `"gdpr_art_93_p_1"` and fell to vector fallback.

**Root-cause measurement:** the query layer grounds a question by showing the
LLM the list of "covered provisions" (provisions that verified statements are
sourced from) and asking it to pick targets. That list was **43** at eval
scale; at corpus scale it is **1,620** (137 of them recitals). The
covered-only retrieval design (Phase-2 plan: "~50 covered provisions") assumed
a small list.

**Claude's initial diagnosis was that this is structural and would hit Gemini
too — that was WRONG (see 12.2).** Recorded here because the correction is the
finding.

### 12.2 Gemini probe — refutes the structural claim; the failure is backend-specific

Three queries (Q01 consent, Q02 medical-access, Q10 health-data-sale) re-run on
**Gemini** against the same corpus graph (`data/graphrag/probe3_gemini.results.jsonl`):

| query | gold | Gemini corpus | intent grounding | recitals retrieved |
|---|---|---|---|---|
| Q01 | COMPLIANT | INSUFFICIENT | art_9/par_1, art_9/par_2/pt_a, art_7 (correct) | 0 |
| Q02 | COMPLIANT | COMPLIANT ✓ | art_9/par_2/pt_h, art_9/par_3 (correct) | 0 |
| Q10 | NON_COMPLIANT | NON_COMPLIANT ✓ | art_9 + aiact art_10/par_5 (correct) | 0 |

Gemini grounds to the **right articles**, retrieves **zero recitals**, and
answers sensibly — same prompt, same 1,620-provision list, opposite outcome
from Mistral. So the large covered list makes intent-grounding *harder* but
does not break it; **the weak model cannot ground against it (collapses to a
bare `"gdpr"` target → recital retrieval → bad synthesis), the capable model
can.** The failure is the backend, not the pipeline.

**One real Gemini regression, explained:** Q01 was COMPLIANT at eval scale,
INSUFFICIENT at corpus scale. Cause: the corpus retrieved the Art 7
consent-condition statements (absent at eval scale), and that extra material
triggered the documented over-caution class (wants the consent-validity layer
verified). More coverage → more caution on that one question — minor and
explainable, not garbage.

**Reading:** this is the model-sensitivity finding (Chung/Mavridis) reproducing
and *amplifying* at corpus scale. At eval scale Mistral's gap was concentrated
in abstention; at corpus scale the harder grounding task widens it because the
weak model now also fails at grounding. A stronger version of the existing
finding, not a defect.

### 12.3 Full corpus gold-50 — results (offline metrics)

Both backends on the same canonical batch (`data/graphrag/gold_run_batch.jsonl`)
against the full corpus graph, scored offline with `score_query.py`.

| metric | eval Gemini | corpus Gemini | corpus Mistral |
|---|---|---|---|
| adherence | 0.920 | **0.780** (39/50) | 0.580 (29/50) |
| macro-F1 | 0.917 | 0.778 | 0.522 |
| grounded (template seed) | — | 47/50 | 24/50 |
| vector_fallback seed | — | 2/50 | 26/50 |
| citation recall | 0.946 | 0.811 | 0.072 |

Files: `data/graphrag/gold_corpus_{gemini,mistral}.{results,metrics}.json(l)`.

**Reading.** Gemini drops 0.920 → 0.780 but this is **not** a retrieval failure
(47/50 grounded via template, near-zero recitals) — it is a *synthesis*
regression from richer corpus retrieval (see 12.4). Mistral collapses to 0.580
because its intent grounding fails on the large covered list: 26/50 fall to
vector fallback, which retrieves recitals, so citation recall craters to 0.072
and verdicts default to NOT_APPLICABLE. Same pipeline, same graph — the gap is
the backend. This reproduces and *amplifies* the eval-scale model-sensitivity
finding (Chung/Mavridis): at eval scale Mistral's gap was concentrated in
abstention; at corpus scale the harder grounding task widens it.

### 12.4 Why Gemini regressed — Q20 and Q25

- **Q20 (90-day logs flagship, INSUFFICIENT → NON_COMPLIANT).** At eval scale
  intent grounded tightly to `art_12/par_1` + `art_5/par_1/pt_e` and ruled
  INSUFFICIENT (correct). At corpus scale intent grounded broader (all of
  Art 12, Annex III, Art 9) AND expansion pulled in the **new
  `erasure_vs_log_retention` conflict edge** (`art_12 CONFLICTS_WITH art_17`,
  added in verification §9.7). Logging now visibly conflicts with *both*
  storage-limitation and erasure, and that extra conflict material pushed the
  model from "depends on facts" to "violation." The conflict pattern added this
  session is directly implicated in the flagship flip — it is a real tension,
  but it changes this verdict.
- **Q25 (small-org exemption, still NON_COMPLIANT — but the coverage gap
  CLOSED).** At eval scale the model asserted the base record-keeping duty whose
  text was *not in the graph* (gap-filling from general knowledge — the
  documented failure). At corpus scale `art_30/par_1`–`par_2` are now retrieved
  and **cited** (`gdpr:art_30/par_1#s2`) — the gap-filling is gone, the pivotal
  claim is grounded. The verdict stays NON_COMPLIANT only because Q25 is a
  genuine INSUFFICIENT/NON_COMPLIANT boundary case (like Q23), not a coverage
  failure. **So the predicted coverage closure happened mechanistically** (the
  Q25/S3 prediction was correct at the grounding level), even though the
  adherence number did not move.

General mechanism: more corpus content → richer retrieval → the capable model
becomes both more over-cautious on some questions (C→INS: Q01a, Q04, Q07, Q08)
and more over-decisive on the INSUFFICIENT boundary (INS→NC: Q20, Q23, Q25).
Retrieval is correct; the synthesis behaviour shifts with the richer context.

### 12.5 Verdict–explanation coherence — a Mistral limitation (one example)

Yoseph spotted that corpus-Mistral Q21b returns verdict **COMPLIANT** while its
explanation concludes "MigrainePredict must comply with the user's deletion
request" — i.e. the label contradicts its own reasoning. This exposed that the
metrics score verdict-label-vs-gold (adherence) and explanation-vs-context
(faithfulness) but **not** verdict-vs-explanation coherence. Yoseph manually
read eval-Gemini, corpus-Gemini, and corpus-Mistral-narrowed and found **no
such contradictions in Gemini** — the incoherence is a **Mistral** weak-model
limitation, observed on this one example (Q21a/Q21b family). Decision (Yoseph):
document it as a Mistral verdict limitation; **do not add a coherence check**.
Related: Q21's yes/no phrasing ("does the company have to comply with the
deletion request?") strains the four-label schema (smoke-stage finding F7),
which compounds the weak model's incoherence.

### 12.6 Vector-narrowing fix (built 2026-07-13)

Root cause of the Mistral collapse: the intent stage is shown the covered-
provision list and asked to pick targets, and that list is 43 at eval scale but
**1,620** at corpus scale — too large for a weak model to ground against.

Fix (`graphrag_query.py`, `NARROW_COVERED = 50`): before the intent call, narrow
the covered list to the 50 most question-similar provisions via the existing
snippet index; only the intent-stage candidate list changes (seeding, expansion,
synthesis untouched). **No-op at eval scale by construction** — eval covered =
43 ≤ 50, so the narrowing branch is never entered and the intent prompt is
byte-identical to the pre-fix code (proven without an LLM run: a 50-query eval
re-run would only measure LLM noise, so it was not spent).

**Result — corpus Mistral, narrowed** (`gold_corpus_mistral_narrowed.*`, Yoseph
ran it): grounding fixed — template seeds **24 → 48**, vector_fallback 26 → 2;
adherence **29 → 35 / 50**, macro-F1 **0.522 → 0.644**. The fix does what it was
designed to for the weak backend. (It is not expected to recover Q20, whose flip
is an expansion-edge effect, not a grounding one.)

**Result — corpus Gemini, narrowed** (`gold_corpus_gemini_narrowed.*`): a
**mixed** result, not a clean pass.

| metric | corpus Gemini | corpus Gemini narrowed |
|---|---|---|
| adherence | 0.780 | **0.800** (no regression) |
| macro-F1 | 0.778 | 0.806 |
| citation recall | 0.811 | **0.577 (REGRESSION)** |

Verdict adherence does not regress (slightly better), but **citation recall
drops 0.811 → 0.577** on Gemini — 15/50 queries cite fewer gold statements.
Cause, from per-query analysis: narrowing the intent candidate list to the top-50
question-similar provisions squeezes out (a) **foundational definitions**
(`art_4/par_1`, `art_4/par_11`, `art_4/par_15`, `art_3/par_1`, `art_2/par_1`) —
they carry no scenario vocabulary so they rank low — and (b) on some paraphrases,
**core operative provisions** (Q01b lost Art 9, Q20 lost the flagship
logging/storage pair). So narrowing also makes *which* provisions get grounded
wording-sensitive. Net: the fix trades citation completeness / traceability
(FR8) for grounding robustness. It is a clear win for the weak backend and a
citation-recall cost for the capable one.

**Open decision (the K=50 tradeoff):** (a) raise `NARROW_COVERED` (e.g. 100–150)
— cheapest lever, likely recovers most citation recall while still cutting
1,620 → ~150 enough to help Mistral; needs a re-run on both backends to confirm;
(b) reserve the definitional provisions (Art 2/3/4 definitions are a small fixed
set) as always-included, plus top-K similar operative provisions; (c) apply
narrowing only for weak backends; (d) accept and document the tradeoff. Not yet
decided.

### 12.7 Decision and interpretation (Yoseph, 2026-07-13)

**Headline backend = Gemini, non-narrowed.** Corpus Gemini 0.780 adherence /
0.811 citation recall is the reported corpus-scale result. Narrowing is kept in
the code (`NARROW_COVERED`, no-op at eval) and reported as a **robustness lever
with a measured tradeoff** — it rescues weak backends (Mistral 0.58 → 0.64,
grounding fixed) at a citation-recall cost on the capable backend — but it is
NOT used for the headline. It is a documented option, not the default story.

**Reading the 0.780 honestly (not a broken pipeline):**
- The eval-scale 0.920 was measured over a 54-statement hand-curated graph that
  covered exactly the gold queries — a tiny, perfectly-matched retrieval space.
  0.780 over 2,271 realistic statements is the *honest* corpus number; the drop
  is the realistic setting showing up, not a regression of a working thing.
- The misses are characterized, not random, and several are contestable gold:
  over-caution (C→INS — defensible caution for a compliance assistant) and
  over-decisiveness on the INSUFFICIENT/NON_COMPLIANT boundary (Q23, Q25 —
  adjudicated at eval scale as genuine ambiguity where gold could defensibly
  flip). Exact-match agreement with one expert on ambiguous boundary items is
  inherently capped below 100%.
- The pipeline does what it was built to do: grounds to the right articles
  (47/50 template, ~0 recitals), retrieves verified statements, abstains on
  missing facts, and closed the predicted coverage gap (Q25). NOTE: an earlier
  version claimed the reportable win was "comparative accuracy + FR8" — the
  three-way result (§12.8) shows accuracy is a WASH vs the baseline at scale and
  citation recall is a weak differentiator. See §12.8 for the corrected framing.

Rejected drastic options (recorded so they are not re-litigated): scrapping the
architecture (no better one identified; matches the report spec; works);
restricting to `applies_to_healthcare` statements (already rejected — a curated
subset makes INSUFFICIENT ambiguous, and retrieval is not the capable backend's
problem); the object-head-noun fix (verification-stage FP class, orthogonal to
query verdicts). K-tuning and head-noun go to the limitations section, not more
engineering.

### 12.8 Three-way corpus comparison (offline)

`baseline_corpus.*` = the vector-only RAG baseline (same covered-only snippet
index, snippets→synthesis, no intent/graph/statements), Gemini backend.

| metric | pipeline-Gemini | vector baseline | pipeline-Mistral |
|---|---|---|---|
| adherence | 0.780 | 0.760 | 0.580 |
| macro-F1 | 0.778 | 0.763 | 0.522 |
| **citation recall** | **0.811** | **0.000** | 0.072 |
| NON_COMPLIANT recall | 0.938 | 0.938 | 0.562 |
| INSUFFICIENT recall | 0.500 | 0.600 | 0.100 |

**The honest finding — verdict accuracy converges at corpus scale.** At eval
scale the pipeline led the baseline on accuracy (0.92 vs 0.82). At corpus scale
that edge nearly vanishes (0.780 vs 0.760): the baseline *improves* at scale
(more indexed provision text → similarity search finds relevant snippets more
often) while the pipeline gets slightly cautious (the richer-retrieval synthesis
effect, §12.4). On the 4-label verdict metric the two systems are effectively
equivalent at corpus scale. This is the central, uncomfortable result and it is
reported as-is.

**CORRECTION (Yoseph, 2026-07-13) — citation recall was over-weighted and is NOT
a strong differentiator.** An earlier version of this section framed the 0.811
vs 0.000 citation-recall gap as "the point." That is wrong: the baseline still
surfaces the *provision text* of the relevant articles, which is a comparable
explanation to a human reader. A statement-ID citation vs the article's text are
not a meaningful explainability difference. Do not rest the contribution on
citation recall.

**What the KG genuinely does that flat retrieval cannot — and its honest scope.**
The one demonstrated capability difference is **cross-regulation conflict
reasoning**: on Q20 (the flagship logging-vs-storage tension) the pipeline
retrieves the `CONFLICTS_WITH` edges and cites *both* GDPR and AI Act provisions;
the baseline retrieves only one regulation (and the wrong provision). A
similarity retriever cannot systematically surface that a duty in one regulation
conflicts with a duty in another, because the two are not text-similar. **But the
honest scope is narrow:** the corpus has only a handful of curated
cross-regulation conflict patterns, and even on Q20 both systems produced the
*wrong* verdict label — so the KG surfaces better/correct evidence (both
regulations + the conflict) without necessarily producing a better label. On the
large majority of queries, KG-backed and flat-retrieval answers are comparable.

**Consequence for the dissertation framing (unresolved — needs a decision).**
The project's contribution cannot rest on the GraphRAG-vs-RAG QA accuracy
comparison, which is a wash at scale. Candidate framings that survive this
result: (i) the **KG-construction methodology itself** (Phase 1 — extraction,
mapping, verification) evaluated on its own terms, with the QA layer as one
demonstration; (ii) the KG as a **structured, queryable compliance asset**
supporting queries flat RAG cannot express (enumerate conflicts, exceptions,
obligations by actor) — beyond the 50 QA prompts; (iii) an as-yet-unmeasured
**groundedness/faithfulness** advantage (the pipeline synthesises only from
verified statements; the baseline can import parametric knowledge — at eval scale
the live faithfulness metric separated them on NOT_APPLICABLE). (iii) is the one
that, if it holds at corpus scale, would be a *broad* (all-query) differentiator
rather than the narrow conflict one; it requires the deferred live run to settle.

### 12.9 Live faithfulness/relevance — the broad differentiator (corpus)

Run 2026-07-13 (Gemini judge, ~2 calls/query) on the pipeline and baseline
corpus runs. Metrics merged into `gold_corpus_gemini.metrics.json` /
`baseline_corpus.metrics.json` (RAG only). Faithfulness = fraction of the explanation's
claims supported by the retrieved context (imported/parametric-knowledge claims
count as unsupported).

| faithfulness by gold label | n | pipeline | baseline |
|---|---|---|---|
| COMPLIANT | 15 | 0.950 | 0.863 |
| NON_COMPLIANT | 16 | 0.952 | 0.916 |
| INSUFFICIENT | 10 | 0.887 | 0.892 |
| **NOT_APPLICABLE** | 9 | **0.944** | **0.697** |
| **OVERALL** | 50 | **0.937** | **0.856** |

Answer relevance is a wash (0.811 vs 0.802). Unsupported claims: pipeline 44,
baseline 56.

**This is the real, broad differentiator (unlike citation recall).** The
pipeline is more grounded across all 50 queries (0.937 vs 0.856), and the gap is
**concentrated on NOT_APPLICABLE (0.944 vs 0.697, +0.25)** — the "is this even
governed?" questions. Biggest per-query deficits are all scope questions: Q29
(anonymous statistics) and Q30 (non-AI FAQ) at baseline 0.40 vs pipeline 1.00.
Mechanism (reproduces the eval-scale finding): the baseline lacks the retrieved
*definitions*, so it reasons about out-of-scope from parametric knowledge
(ungrounded); the pipeline retrieves the definitions of "personal data" / "AI
system" and grounds its scope determination in them — the no-inference-from-
silence / bounded-knowledge property the architecture was built for.

**Honest limits:** average gap is modest (+0.08); on INSUFFICIENT it is a tie
(baseline 0.892 vs pipeline 0.887); on NON_COMPLIANT the gap is small (+0.036);
the pipeline is not perfect (44 unsupported claims, min faithfulness 0.50). So
this is a *modest-average, decisive-on-scope* advantage, not a blowout.

### 12.10 The corpus-scale contribution (honest, calibrated)

The GraphRAG-vs-RAG comparison does NOT show a verdict-accuracy win (wash at
scale) and must not be framed as one. The defensible contribution is:
1. **Groundedness:** at comparable accuracy the pipeline's compliance reasoning
   is more faithful to verified law (0.937 vs 0.856), decisively so on scope
   determination (NA 0.944 vs 0.697) — a broad, measured property, not 3 items.
2. **Cross-regulation conflict reasoning** the baseline structurally cannot do
   (§12.8; narrow but real — the KG surfaces both regulations' duties + the
   conflict edge).
3. **The KG-construction methodology** (Phase 1), evaluated on its own terms,
   independent of the QA comparison.

Headline: *at comparable verdict accuracy, GraphRAG produces more faithful,
grounded, conflict-aware compliance answers — most strongly on scope — while the
vector baseline reasons from unretrieved knowledge.* In a domain where
groundedness and auditability are regulatory requirements, that is the
contribution. Corpus query eval is complete.

---

*(Chronology note: the fixes below run in the order they happened — clean-render
(§12.11, 19:37) → bare-LLM comparison (§12.12, 21:39) → the "not actually RAG"
realisation + dense-seed (§12.13, 22:39). Timestamps from the `*.results.jsonl`
files.)*

## 12.11 Clean-render fix — removing garbled data from the synthesis context

Diagnostic (Yoseph's hypothesis: "same LLM, different answers with vs without
the KG → we must be presenting it bad data"). Reconstructing the exact synthesis
context for Q25 showed the pipeline was feeding the LLM **garbled statement
renderings** from the `cc.serialize` structured gloss — e.g. `gdpr:art_30/par_1#s1`
rendered as *"The controller shall contain all of the following information"*
(the extraction's duty-bearer inference had rewritten the subject "That record"
→ "the controller"; the gloss then produced nonsense). Fix (`render_statement`):
drop the lossy `cc.serialize` gloss; present the **authoritative source text**
(anchor sentence) plus the **parent chapeau** (`parent_texts`) so a sub-point
is framed by its parent instead of floating.

**Result — KG pipeline (Gemini) clean-render: adherence 0.780 → 0.840**
(macro-F1 0.778 → 0.828). Fixed the flagship Q20 (NON_COMPLIANT → INSUFFICIENT,
correct) *without* removing the erasure conflict pattern. Change breakdown: +7
fixed (Q04/Q08/Q01a over-caution; Q20/Q23 over-decisiveness; Q03b/Q13b), −4
broken (Q15/Q01b/Q21a/Q21b). Cost: citation recall dropped **0.811 → 0.595**
(statement-level, `score_query.py`; the cleaner context makes the LLM cite
fewer). This is a principled fix (it
removes genuinely garbled data, defensible in a viva), not results-driven.
**This is the best KG configuration (highest adherence) and the one to report.**
OPEN ITEMS on it (not yet done): (a) single-run, +3 net — report as ≈0.80–0.84
with run-to-run variance, no 5-run mean taken; (b) its **faithfulness was not
measured** — the 0.937 figure (§12.16) belongs to the *pre-fix* run
`gold_corpus_gemini`, so if clean-render is the headline its live
faithfulness/relevance should be re-run. Both are live-LLM runs (API cost).

## 12.12 Bare-LLM comparator — the decisive comparison (2026-07-13)

After the clean-render fix, the evaluation design was corrected on Yoseph's
reasoning: comparing the KG pipeline against a *vector-RAG baseline* is weak,
because the brief already requires a RAG system — comparing one RAG against
another does not isolate the value of the KG. **The right baseline is a bare
LLM with NO retrieval and NO KG** (it does not "ignore" retrieval — it has none;
it answers purely from its own parametric knowledge of GDPR / the AI Act). A
`--llm-only` mode was added to `graphrag_query.py` (same 4-label
`ComplianceVerdict` schema, no context); the citation format was pinned to the
project IRI convention so citations are comparable.

**Result — bare LLM (Gemini): adherence 0.900, macro-F1 0.904.** It **beats**
the KG pipeline (0.78–0.84) on verdict accuracy.

**Why (the central finding):** GDPR and the EU AI Act are extensively in the
LLM's training data — the model has effectively *memorized* them. On a task of
deciding compliance labels for well-known regulations, an LLM answers from
parametric knowledge and does it better than a retrieval-augmented system,
because retrieval adds noise/over-commitment a confident memory does not have.
Retrieval augmentation (RAG *or* KG) only helps when the knowledge is *outside*
the model's training — private policies, niche rules, or newly-changed law.
This is not a defect of this pipeline; it is a property of the task, and it
maps onto the brief's own motivation (keeping pace with *legislative change*),
which an evaluation built on stable, famous regulations cannot exercise.

## 12.13 "It is not actually RAG" — the retrieval mechanism, and the provision fix

Key correction (Yoseph): the system had been called "KG+RAG" throughout, but its
**primary retrieval is not RAG.** The intent stage shows the LLM a list of
~1,620 **bare provision IRIs** and asks it to pick the relevant ones *from its
own knowledge of what each article number contains* (47/50 queries used this
"template" path; dense vector retrieval was only a rarely-used fallback, 2/50).
So retrieval was **LLM recall of article numbers**, not dense text retrieval.
This explains the paraphrase sensitivity (§12.14) and the wrong-provision
citations: it is the same parametric-knowledge crutch as the bare LLM.

**The "provision fix" (`--dense-seed`):** seed selection now retrieves the
`DENSE_SEED_K` question-nearest provisions by **text-embedding similarity** and
seeds their statements (then graph-expands) — genuine dense RAG-over-KG.
Recitals (`rct_*`, non-binding preamble whose plain language out-ranks formal
article text) are excluded from seeding.

**STATUS — built and probe-tested, NOT fully evaluated (honest gap).** The
`--dense-seed` mode is in `graphrag_query.py` and was smoke/probe-tested. Only a
**Mistral** dense-seed batch was run (`gold_corpus_mistral_denseseed.results.jsonl`,
by Yoseph) and it was poor — NOT_APPLICABLE verdicts citing recitals. **No full
Gemini dense-seed gold-50 run was done.** It was not pursued further because the
recall@k analysis (§12.14) showed dense retrieval is *ceiling-limited* on this
corpus (recall caps ~0.75; a hybrid-20→rerank→top-N simulation showed recall
~0.6, no gain over the current pipeline), so a paid Gemini dense-seed run was
judged unlikely to improve the citation numbers. **If a measured RAG-over-KG
result is wanted for the write-up, the outstanding action is one Gemini
`--dense-seed` gold-50 run** — but the honest expectation (from §12.14) is
citation numbers near the current pipeline, bounded by the same retrieval
ceiling, not an improvement.

## 12.14 Retrieval is the bottleneck — the recall@k curve and six methods tested

Paraphrase sensitivity at corpus scale is **7–8/10 consistent trios** (down from
10/10 at eval scale). Root cause, confirmed by inspecting intent targets: the
same scenario, reworded, grounds to *different* provisions (Q20/Q20a/Q20b each
grounded to different, often irrelevant, provisions; none hit the correct
flagship pair). The retrieval — whether LLM-recall or dense — does not reliably
find the correct operative provisions.

**Recall@k curve (BGE-M3), over the 47 gold queries with gold-cited provisions.
Reproducible: `python retrieval_analysis.py` → `data/graphrag/retrieval_analysis.json`.**

| k | dense recall@k | hybrid recall@k |
|---|---|---|
| 1 | 0.181 | 0.202 |
| 3 | 0.280 | **0.365** |
| 5 | 0.362 | **0.479** |
| 10 | 0.434 | 0.539 |
| 20 | 0.619 | 0.633 |
| 30 | 0.677 | 0.665 |
| 50 | 0.697 | 0.715 |
| 100 | 0.746 | 0.750 |

The curve separates two problems: (1) a **ranking problem** — recall@3 is only
0.28–0.37, so the right provisions are rarely in the top few the LLM reads; and
(2) a **hard ceiling** — recall plateaus at **~0.75 even at k=100**, i.e. **~25%
of gold provisions are unretrievable at any depth** with these embeddings. (The
probe rankings below are also in `retrieval_analysis.json`.)

**Every standard retrieval fix was tested (free, on probe/gold queries); none
fixes it, because each is a general-purpose tool and the problem is domain
mismatch:**

- **(0) Query instruction prefix** (bge-en-v1.5 asymmetric-retrieval prefix on
  the query side). Correct to add, but changed scores marginally and did **not**
  change rankings — logging still → `art_19` not `art_12`; consent still no
  `art_9`. Only the keyword-obvious case (erasure → `art_17`) worked.
- **(1) Stronger embeddings — BGE-M3** (1024-dim, 8192 ctx; instruction-free;
  the prefix is disabled for it). Barely moved it — `art_12` rose to #4 on the
  logging probe; consent still surfaced recitals + `art_49`, no `art_9`.
- **(2) Hybrid dense+sparse (BGE-M3 sparse, BM25-like)** — the one genuine
  signal: the **sparse component surfaces `art_9/par_2/pt_a` for the consent
  query (#1 sparse, #2 hybrid) that dense missed entirely.** Improves low-k
  recall (recall@3 0.25→0.37, @5 0.36→0.48). But partial: "sell health data →
  Art 9" still fails (zero keyword *or* semantic overlap), and the ceiling is
  unchanged.
- **(3) Cross-encoder reranking (`bge-reranker-base`)** — **mixed.** Helped
  consent (`art_9` → top-3 from a wide candidate set) but *hurt* logging
  (`art_16` #1) and erasure (Art 15/19 displaced Art 17 sub-points), because a
  general-domain reranker carries the same domain mismatch.
- **(4) Wide-k + rerank (hybrid-20 → rerank → top-N)** — simulated for free:
  recall caps at ~0.59–0.63 (≈ the current pipeline), i.e. it does **not** raise
  recall above the ceiling.
- **(5) Citation tightening** (post-filter citations to those named in the
  explanation; provision-level) — recall 0.628 → 0.564, precision 0.247 → 0.282.
  Trades recall for a sliver of precision; not a fix. (See §12.15 on levels.)

**The established fix is domain-adapted retrieval/reranking**, which is not
available off the shelf: **RegGuard's ReLACE** (arXiv 2601.17826, Jan 2026) is a
`bge-reranker-base` **fine-tuned on in-domain regulatory reranking data** — it is
*not* publicly released, so it cannot be imported; replicating it is a
fine-tuning project (needs a regulatory query→provision training set) out of the
project timeframe. **LegalBERT** (Chalkidis et al. 2020) is a masked LM, not a
retriever, and would likewise need retrieval fine-tuning. Both are legitimate
future-work citations; neither is a drop-in.

## 12.15 Citation quality — the metric, and the single root cause

**Headline citation numbers (KG pipeline) are the statement-level figures from
`score_query.py` — the trusted scorer, already in the `*.metrics.json` files;
NOT recomputed here:**

| KG pipeline (statement-level, `score_query.py`) | recall | precision |
|---|---|---|
| pre-fix (Gemini) | 0.811 | 0.265 |
| **clean-render (Gemini)** | **0.595** | **0.246** |

These match citations exactly (`gdpr:art_9/par_1#s1`). For the **bare LLM** and
the **vector-RAG baseline**, this same statement-level metric reads **0.000** —
but that is a **granularity artifact, not "no citations":** they cite
provision-level IRIs (`gdpr:art_9/par_1`, no `#sN`), which never string-match
the statement-level gold.

**To compare those systems on citations at all, a coarser PROVISION-level
metric is used** — strip the `#sN` suffix on both sides (reproducible:
`python citation_analysis.py` → `data/graphrag/citation_analysis.json`).
**IMPORTANT: provision-level matching is coarser — any statement of the right
article counts as a hit — so it systematically reads HIGHER than the
statement-level headline.** It exists only to give the bare LLM and baseline a
non-artifact number; it is **not** the KG's headline metric.

| system (provision-level — coarser, reads higher) | recall | precision |
|---|---|---|
| bare LLM (Gemini) | 0.606 | 0.467 |
| vector-RAG baseline | 0.160 | 0.149 |
| KG clean-render (shown here only for like-for-like) | 0.628 | 0.247 |
| KG pre-fix (over-citing, fp=210) | 0.851 | 0.276 |

The KG clean-render reads **0.628 provision-level vs its 0.595 statement-level
headline** — the coarser metric inflating it, exactly as expected; the two must
not be mixed. **Citation tightening** (post-filter to provisions named in the
explanation, provision-level, clean-render) moved recall 0.628 → 0.564 and
precision 0.247 → 0.282 — trades recall for a sliver of precision; not a fix.

**Both bad numbers have one root cause: retrieval surfaces the wrong
provisions.** Recall is low because the right provisions are missed (the
ceiling); precision is low because the wrong provisions are retrieved and then
faithfully cited. Post-processing cannot fix it (the citations correctly reflect
what was retrieved). Only a better (domain-adapted) retriever moves both.

**Important distinction for the write-up (this is what "grounded" honestly
means here):** the KG pipeline's citations are **not invented** — it only cites
statements present in its retrieved context, and each is **traceable to a
verified, extracted provision** with source text and provenance; a bare LLM's
from-memory citations are not verifiable in this way. What is limited is
retrieval *accuracy* (which provisions get surfaced), quantified by the
recall@k curve — not citation *integrity*.

## 12.16 Faithfulness (live) — the one axis where the KG helps a capable model

RAGAS-style faithfulness (fraction of explanation claims supported by the
retrieved context; parametric-knowledge claims count as unsupported), Gemini
judge, corpus runs:

| faithfulness (overall) | value |
|---|---|
| Gemini KG pipeline | **0.937** |
| Gemini vector-RAG baseline | 0.856 |
| Mistral vector-RAG baseline | 0.740 |
| Mistral KG pipeline (narrowed) | 0.687 |

The KG pipeline is the most faithful **with a capable backend** (0.937 vs 0.856,
concentrated on NOT_APPLICABLE / scope questions where the baseline reasons from
imported knowledge). But **the KG *hurts* a weak backend's faithfulness**
(Mistral KG 0.687 < Mistral baseline 0.740) — the richer structured context
overwhelms the weak model, which generates more unsupported claims. So: the KG
amplifies a capable model's grounding and overwhelms a weak one — it is not a
substitute for model capability. Caveat: a bare-LLM faithfulness is ~undefined
(no context), so faithfulness is a pipeline-vs-RAG axis, not a pipeline-vs-bare-
LLM one.

## 12.17 Full corpus results table (consolidated)

| system | what it is | verdict adherence | citation recall / prec (prov.) | faithfulness |
|---|---|---|---|---|
| bare LLM (Gemini) | no retrieval, no KG | **0.900** | 0.606 / 0.467 | — (no context) |
| KG pipeline clean-render (Gemini) | KG + LLM-provision-select + expansion | 0.840 | 0.628 / 0.247 | 0.937 |
| KG pipeline pre-fix (Gemini) | as above, garbled render | 0.780 | 0.851 / 0.276 | 0.937 |
| vector-RAG baseline (Gemini) | dense RAG, no KG | 0.760 | 0.160 / 0.149 | 0.856 |
| KG pipeline Mistral (narrowed) | as above, weak backend | 0.700 | — | 0.687 |
| Mistral vector-RAG baseline | dense RAG, weak backend | 0.900* | — | 0.740 |
| KG pipeline Mistral (non-narrow) | grounding collapsed | 0.580 | — | — |

*Mistral baseline's high adherence is the memorization artifact (high accuracy,
weak faithfulness 0.740) — evidence that verdict accuracy is contaminated by
parametric knowledge and cannot alone certify a compliance system.

**The citation column is PROVISION-level** (coarser — strips `#sN`) purely so
the bare LLM and vector baseline get a comparable number rather than the 0.000
statement-level artifact (they cite provisions, not statement IDs). Because it
is coarser it reads higher than the headline. **The KG pipeline's official
citation metric is the statement-level `score_query.py` figure — clean-render
0.595 / 0.246, pre-fix 0.811 / 0.265 — reported in §12.15, not this column.**

## 12.18 Honest contribution, limitations, and requirements mapping (write-up)

**The honest thesis (verbatim, for the evaluation chapter):**

> A bare LLM can *guess* compliance labels accurately from memorized law (0.90)
> — but it cannot ground, cite, verify, or update those judgments. The KG+RAG
> system trades a few points of raw label accuracy for answers that are
> **grounded in extracted, verified, source-linked, and maintainable**
> regulatory knowledge — which is the actual requirement for a safety-critical
> compliance system. Verdict accuracy from an unverifiable memory is the wrong
> metric; groundedness and auditability are the right ones.

> This **localizes where a compliance KG adds value**: coverage, verifiability,
> and maintainability for evolving / out-of-training regulation — not raw
> accuracy on static, famous law. On GDPR and the AI Act, which the model has
> memorized, retrieval augmentation shows its benefit in faithfulness and
> traceability, not in verdict accuracy.

**The retrieval limitation (verbatim):**

> Off-the-shelf general-purpose retrievers and rerankers (BGE-M3 dense, dense+
> sparse hybrid, `bge-reranker-base`) do not reliably retrieve the correct
> operative provisions over dense EU-regulatory text — demonstrated empirically
> by a recall@k curve (ceiling ~0.75; recall@3 ≈ 0.25–0.37) and by six tested
> methods. The established fix is domain-adapted reranking (RegGuard's ReLACE, a
> regulatory fine-tune of `bge-reranker-base`), identified as future work but
> not feasible within the project timeframe.

**Cross-regulation conflict reasoning (a KG-only capability, contribution):**
The KG encodes verified `CONFLICTS_WITH` edges between provisions of *different*
regulations — the flagship AI Act Art 12 logging ↔ GDPR Art 5(1)(e) storage
limitation, plus (added this stage, literature-grounded) Art 9(1) GDPR ↔
Art 10(5) AI Act (special-category vs bias-detection) and Art 17 GDPR ↔ Art 12
AI Act (erasure vs log retention). On the flagship query the pipeline retrieves
these edges and cites **both** regulations' operative duties, whereas the vector
baseline retrieves only one regulation. **This is a capability a flat retriever
structurally cannot provide** (two conflicting provisions from different
regulations are neither text-similar to each other nor to the query, so
similarity retrieval will not surface both) and a bare LLM does not do reliably.
Honest scope: narrow (a handful of curated patterns) and it surfaces
better/correct *evidence* without necessarily changing the 4-label verdict —
but it is a genuine, dual-regulation compliance capability that maps directly
onto the MigrainePredict scenario's central GDPR↔AI-Act tension.

**Requirements mapping (against the project brief):**
1. *"LLM-based extraction pipeline … compliance requirements as ontology
   components"* — **satisfied** (extraction F1 ≈ 0.93 on gold; DPV/AIRO/VAIR
   grounding; full-corpus KG; verification stage). This is the core contribution
   and is unaffected by the QA result.
2. *"compliance-aware RAG system … NL queries validated against ontology
   constraints"* — **satisfied** (the system is built and answers grounded
   verdicts). NOTE: the brief asks to *build* a RAG system, **not** to beat a
   baseline — the baseline comparisons are added rigor, and a wash/negative
   comparison still satisfies "evaluate your system."
3. *"evaluate … web-based evaluation interface for decision makers + automated
   metrics"* — **automated metrics: over-satisfied** (adherence, per-label
   P/R/F1/F2, faithfulness, answer relevance, paraphrase sensitivity, citation
   metrics, recall@k, model×architecture ablation). **Web-based evaluation
   interface: NOT built — the one genuine outstanding deliverable.**

**Bottom line for the dissertation:** the KG construction is the contribution;
the QA evaluation is a rigorous, honestly-characterized result showing (a) LLMs
already know famous regulations, so retrieval does not raise verdict accuracy,
(b) the KG's value is grounded/faithful/traceable/maintainable answers with a
capable backend, (c) the KG uniquely enables **cross-regulation conflict
reasoning** over the GDPR↔AI-Act tension (above), which flat retrieval and a
bare LLM cannot provide, and (d) retrieval over dense legal text with
general-purpose models is a hard, quantified limitation with a cited
domain-adaptation fix as future work. This is a defensible negative/nuanced
result backed by extensive evidence, not an unexplained set of bad numbers.
