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
  MigrainePredict retain operational logs for 90 days?":
  - Verdict **INSUFFICIENT** — which is the outcome the report itself
    predicts for this question, and it was reached via the intended path:
    deterministic template, correct provisions, all three conflict-pair
    statements and all three relationship edges retrieved, both citations
    real, result auto-routed to the review queue.
  - Three observations logged for later, none blocking:
    1. One sentence of the explanation drew on general knowledge (that such
       conflicts are often resolved by anonymising log data) instead of the
       retrieved context. The citation list stayed clean. This is exactly
       what the faithfulness metric will measure — treat as a finding, not a
       prompt patch.
    2. The model read the AI Act duty ("must technically allow automatic
       recording of events") as implying logs must be *kept* for the
       system's lifetime — a stretch beyond what the statement says, though
       hedged and harmless here.
    3. Its "what's missing" list differs from the report's example (it asks
       about retention purpose and system lifetime, not pseudonymisation) —
       correctly so, because the graph contains no pseudonymisation
       exception for it to ask about.

## 5. Next

1. Remaining smoke queries (~4: special-category-data consent, right to
   erasure and its exceptions, high-risk classification / Annex III, plus one
   deliberately off-topic question to test NOT_APPLICABLE). Review results
   together before scaling.
2. Draft the gold query set (~30 + paraphrases) for review.
3. Evaluation harness + vector-only baseline.
4. Expert-review worksheet (Echenim's five dimensions) for Skein.
