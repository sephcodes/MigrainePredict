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

1. Query-review worksheet structure (proposed, pending Yoseph's go-ahead;
   mirrors the verification-worksheet HITL pattern), seeded with the §7
   proposals for his review.
2. Draft the gold query set (~30 + paraphrases) for review, incorporating
   F5–F7.
3. Evaluation harness + vector-only baseline (same snippet index).
4. Expert-review worksheet (Echenim's five dimensions) for Skein.
