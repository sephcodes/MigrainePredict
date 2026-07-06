#!/usr/bin/env python3
"""
graphrag_query.py -- Phase 2 GraphRAG query layer (report section 5.3.2).

Answers a natural-language compliance question against the verified knowledge
graph (Statement:Verified nodes only) and returns a 4-label verdict with an
explanation trace citing the statements and provisions it used.

Pipeline (plain Python stages, one call each -- no LangGraph, per confirmed
design decision 2026-07-05):

  1. classify_intent   LLM structured output -> mode (advisory|audit),
                       target provisions (grounded in the covered-provision
                       list, so it cannot invent articles), scenario facts.
  2. seed selection    HYBRID (confirmed decision):
                       a. deterministic article-conditioned template when the
                          intent names covered provisions (prefix match);
                       b. otherwise LLM-generated read-only Cypher, validated
                          with EXPLAIN and self-corrected on the parser error,
                          up to MAX_CYPHER_ATTEMPTS (Echenim 18%->0% loop --
                          every attempt is audit-logged for that comparison);
                       c. if that fails, vector fallback: nearest covered
                          provisions to the question seed the same template.
  3. expand            deterministic traversal from the seed statements over
                       REFERS_TO / EXCEPTION_OF / CONFLICTS_WITH so exceptions,
                       cross-references and the cross-regulation conflict pair
                       arrive in the same retrieval pass.
  4. snippets          vector search (bge-small-en-v1.5, in-process, cached to
                       data/graphrag/) over COVERED provisions only -- the
                       synthesis can never cite text the KG does not know,
                       which supports no-inference-from-silence.
  5. synthesize        Gemini structured output -> verdict in {COMPLIANT,
                       NON_COMPLIANT, INSUFFICIENT, NOT_APPLICABLE} +
                       explanation + cited ids (must come from the context).
  6. route             INSUFFICIENT verdicts appended to the JSONL review
                       queue (runtime HITL, report FR8/FR9).

Advisory mode only is implemented; the intent schema carries `mode` and the
template table is extensible so artefact-audit can be added if the Skein
vendor policy arrives (audit-ready design, confirmed decision).

Every LLM call, seed path, Cypher attempt and verdict is audit-logged to
data/graphrag/audit_log.jsonl (NFR1), same event shape as load_candidates.

Usage:
  python graphrag_query.py "Can MigrainePredict retain operational logs for 90 days?"
  python graphrag_query.py --batch queries.jsonl        # {"query_id","question"} per line
  python graphrag_query.py --check                      # no-LLM structural self-check
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
from typing import Literal, Optional

from langchain_core.prompts import ChatPromptTemplate
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

import cycle_consistency as cc
import extract_min as em

GRAPHRAG_DIR = "data/graphrag"
AUDIT_LOG = f"{GRAPHRAG_DIR}/audit_log.jsonl"
REVIEW_QUEUE = f"{GRAPHRAG_DIR}/review_queue.jsonl"
RESULTS_LOG = f"{GRAPHRAG_DIR}/results.jsonl"
INDEX_META = f"{GRAPHRAG_DIR}/snippet_index.json"
INDEX_VECS = f"{GRAPHRAG_DIR}/snippet_index.npy"

EMBED_MODEL = "BAAI/bge-small-en-v1.5"  # same model as build_content_candidates
TOPK_SNIPPETS = 5
MAX_CYPHER_ATTEMPTS = 3   # self-correction bound (Echenim & Joshi loop)
MAX_SEEDS = 100

NEO4J_URI = os.environ.get("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "MigrainePredict")


def audit(component, event, **payload):
    """NFR1: one timestamped JSONL event per pipeline action."""
    os.makedirs(GRAPHRAG_DIR, exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "component": component, "event": event, **payload}) + "\n")


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------
class QueryIntent(BaseModel):
    """What the question is asking, grounded in the covered-provision list."""
    mode: Literal["advisory", "audit"] = Field(
        description="advisory = does a described activity/behaviour comply; "
                    "audit = does a supplied document/policy cover requirements")
    target_provisions: list[str] = Field(
        description="Provision IRIs (or IRI prefixes) FROM THE COVERED LIST "
                    "that the question is about. Empty if the question names "
                    "no covered provision and implies none.")
    scenario_facts: list[str] = Field(
        description="Concrete facts the question asserts about the scenario, "
                    "one per entry, verbatim where possible. Do not add facts "
                    "the question does not state.")
    rationale: str = Field(description="One sentence: why these targets.")


class GeneratedCypher(BaseModel):
    cypher: str = Field(description="A single read-only Cypher query.")
    rationale: str = Field(description="One sentence: what the query selects.")


class ComplianceVerdict(BaseModel):
    verdict: Literal["COMPLIANT", "NON_COMPLIANT", "INSUFFICIENT",
                     "NOT_APPLICABLE"]
    explanation: str = Field(
        description="Plain-English explanation trace: what the retrieved "
                    "obligations/permissions/prohibitions/dispensations say, "
                    "how they apply to the scenario facts, and how any "
                    "exception or conflict edge affects the outcome.")
    cited: list[str] = Field(
        description="statement_ids and provision IRIs USED in the reasoning. "
                    "Only ids that appear in the provided context.")
    missing_information: Optional[str] = Field(
        default=None,
        description="Only for INSUFFICIENT: the specific fact(s) that would "
                    "resolve the verdict.")


# ---------------------------------------------------------------------------
# Prompts (inline, version-controlled with the script, as in extract_min)
# ---------------------------------------------------------------------------
INTENT_SYSTEM = """\
You classify a natural-language compliance question about MigrainePredict, a
wearable healthcare AI system for predicting migraine attacks, governed by
both the GDPR and the EU AI Act.

The knowledge graph covers ONLY the provisions listed below ("covered
provisions"). Identify which covered provisions the question is about.

Rules:
- target_provisions entries MUST be drawn from the covered list (a full IRI,
  or an article-level prefix of one, e.g. "gdpr:art_9"). Never invent IRIs.
- Include provisions the question implies as well as those it names: a
  question about health-data consent implies gdpr:art_9; retention/storage of
  personal data implies gdpr:art_5/par_1/pt_e; logging of a high-risk AI
  system implies aiact:art_12.
- If the question is about something no covered provision addresses, return
  an empty target list -- do NOT stretch a loosely-related provision.
- mode is "audit" only when the question asks whether a supplied document or
  policy text covers requirements; otherwise "advisory".
- scenario_facts: only facts the question states. No speculation.

Covered provisions:
{covered}
"""

INTENT_USER = "Question: {question}"

CYPHER_SYSTEM = """\
You write ONE read-only Cypher query for Neo4j to find the statements in a
regulatory knowledge graph that are relevant to a compliance question.

Graph schema:
- (:Statement:Verified) properties: statement_id, statement_class
  ('DEONTIC'|'DEFINITIONAL'|'APPLICABILITY'), modality ('OBLIGATION'|
  'PROHIBITION'|'PERMISSION'|'DISPENSATION', deontic only), subject_text,
  predicate_text, object_text, condition_text (lists of strings), term,
  definition_value (definitional), scope_type, polarity, applies_to_value
  (applicability), anchor (source sentence), source_article.
- (:Provision) properties: iri (e.g. 'gdpr:art_5/par_1/pt_e',
  'aiact:art_12/par_1'), text.
- (:Concept) properties: iri, label. Concept hierarchy: (:Concept)-[:BROADER]->(:Concept).
- (s:Statement)-[:SOURCED_FROM]->(:Provision)
- (s)-[:HAS_SUBJECT|HAS_PREDICATE|HAS_OBJECT|HAS_CONDITION]->(:Concept)
- (s)-[:REFERS_TO]->(:Provision or :Statement)
- (s)-[:EXCEPTION_OF]->(s2)   s derogates from s2
- (s)-[:CONFLICTS_WITH]->(s2) cross-regulation conflict

Hard requirements:
- Match statements with the :Verified label only.
- The query MUST end with: RETURN DISTINCT s.statement_id AS statement_id
  (where s is the relevant statement variable), optionally with LIMIT.
- Read-only: no CREATE/MERGE/SET/DELETE/REMOVE/DROP/CALL.
- Prefer matching on Concept labels/iris or text properties with
  toLower(...) CONTAINS, not on exact free-text equality.
"""

CYPHER_USER = """\
Question: {question}
Scenario facts: {facts}
{feedback}"""

SYNTH_SYSTEM = """\
You are a compliance analyst for MigrainePredict, a wearable healthcare AI
system for predicting migraine attacks (high-risk under EU AI Act Annex III;
its health data is special-category under GDPR Article 9). Decide a verdict
for the question using ONLY the provided context (retrieved statements, their
relationship edges, and provision text snippets).

Verdict labels:
- COMPLIANT: the scenario facts satisfy every retrieved requirement that
  applies to them.
- NON_COMPLIANT: a scenario fact violates a retrieved obligation or
  prohibition, with no retrieved exception or dispensation that lifts it.
- INSUFFICIENT: the retrieved requirements apply, but the scenario is missing
  a fact needed to decide (say which fact in missing_information).
- NOT_APPLICABLE: no retrieved requirement governs the scenario.

Rules:
- NO INFERENCE FROM SILENCE: never infer a violation because the scenario or
  the context does not mention something. Missing scenario detail ->
  INSUFFICIENT; no governing requirement -> NOT_APPLICABLE.
- Respect the edges: EXCEPTION_OF means the source statement derogates from
  the target; CONFLICTS_WITH marks a cross-regulation conflict -- if both
  sides of a conflict apply, explain the tension and what conditions how it
  resolves (do not silently pick a side).
- cited may only contain statement_ids / provision IRIs present in the
  context.
- Write the explanation in plain English: what each relevant statement says,
  whether the scenario meets it, and why the verdict follows.
"""

SYNTH_USER = """\
Question: {question}
Mode: {mode}
Scenario facts:
{facts}

Retrieved statements:
{statements}

Relationships between retrieved statements:
{edges}

Provision text snippets (vector-retrieved, covered provisions only):
{snippets}
"""


def build_query_chains(backend: str):
    llm = em._llm(backend)
    intent_chain = ChatPromptTemplate.from_messages(
        [("system", INTENT_SYSTEM), ("user", INTENT_USER)]
    ) | llm.with_structured_output(QueryIntent)
    cypher_chain = ChatPromptTemplate.from_messages(
        [("system", CYPHER_SYSTEM), ("user", CYPHER_USER)]
    ) | llm.with_structured_output(GeneratedCypher)
    synth_chain = ChatPromptTemplate.from_messages(
        [("system", SYNTH_SYSTEM), ("user", SYNTH_USER)]
    ) | llm.with_structured_output(ComplianceVerdict)
    return intent_chain, cypher_chain, synth_chain


# ---------------------------------------------------------------------------
# Covered provisions + snippet index (covered-only, cached, in-process)
# ---------------------------------------------------------------------------
def covered_provisions(sess):
    """Provisions that verified statements are sourced from (with text)."""
    rows = sess.run(
        "MATCH (:Statement:Verified)-[:SOURCED_FROM]->(p:Provision) "
        "RETURN DISTINCT p.iri AS iri, p.text AS text ORDER BY iri")
    return [(r["iri"], r["text"]) for r in rows]


_EMBEDDER = None


def _embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        from sentence_transformers import SentenceTransformer
        _EMBEDDER = SentenceTransformer(EMBED_MODEL)
    return _EMBEDDER


def load_snippet_index(provisions):
    """Embed covered provision texts once; rebuild only when they change."""
    import numpy as np
    entries = [(iri, text) for iri, text in provisions if text]
    digest = hashlib.sha256(json.dumps(entries).encode()).hexdigest()
    if os.path.exists(INDEX_META) and os.path.exists(INDEX_VECS):
        meta = json.load(open(INDEX_META))
        if meta.get("digest") == digest:
            return meta["entries"], np.load(INDEX_VECS)
    vecs = _embedder().encode([f"{iri}: {text}" for iri, text in entries],
                              normalize_embeddings=True,
                              show_progress_bar=False)
    os.makedirs(GRAPHRAG_DIR, exist_ok=True)
    json.dump({"digest": digest, "entries": entries}, open(INDEX_META, "w"))
    np.save(INDEX_VECS, vecs)
    return entries, vecs


def snippet_search(entries, vecs, query, k=TOPK_SNIPPETS):
    q = _embedder().encode([query], normalize_embeddings=True)[0]
    sims = vecs @ q
    order = sims.argsort()[::-1][:k]
    return [(entries[i][0], entries[i][1], float(sims[i])) for i in order]


# ---------------------------------------------------------------------------
# Seed selection (hybrid): template -> LLM Cypher -> vector fallback
# ---------------------------------------------------------------------------
# Article-conditioned template library (Chattoraj pattern). Deterministic and
# parameterised; each entry maps a recognised intent shape to a seed query.
# Extensible: smoke-query results decide what earns a dedicated entry; an
# artefact-audit template slots in here if the Skein vendor policy arrives.
SEED_BY_PREFIX = (
    "MATCH (s:Statement:Verified)-[:SOURCED_FROM]->(p:Provision) "
    "WHERE any(pref IN $prefixes WHERE p.iri STARTS WITH pref) "
    "RETURN DISTINCT s.statement_id AS statement_id")

_WRITE_RE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|CALL|LOAD\s+CSV)\b", re.I)


def seed_by_template(sess, prefixes):
    rows = sess.run(SEED_BY_PREFIX, prefixes=prefixes)
    return [r["statement_id"] for r in rows][:MAX_SEEDS]


def seed_by_llm_cypher(sess, cypher_chain, question, intent, attempts_log):
    """LLM-generated Cypher with EXPLAIN dry-run + self-correction <= 3
    attempts, parser errors fed back (Echenim loop; attempts audit-logged)."""
    feedback = ""
    for attempt in range(1, MAX_CYPHER_ATTEMPTS + 1):
        gen = em._retry_invoke(cypher_chain, {
            "question": question,
            "facts": "; ".join(intent.scenario_facts) or "(none)",
            "feedback": feedback,
        }, label="cypher generation")
        cypher = gen.cypher.strip().rstrip(";")
        entry = {"attempt": attempt, "cypher": cypher, "error": None}
        attempts_log.append(entry)
        if _WRITE_RE.search(cypher):
            entry["error"] = "rejected: query is not read-only"
        else:
            try:
                sess.run("EXPLAIN " + cypher).consume()
                rows = sess.run(cypher)
                seeds = [r["statement_id"] for r in rows
                         if r.get("statement_id")][:MAX_SEEDS]
                audit("graphrag_query", "cypher_attempt", attempt=attempt,
                      ok=True, seeds=len(seeds))
                return seeds, cypher
            except Exception as e:
                entry["error"] = str(e)[:500]
        audit("graphrag_query", "cypher_attempt", attempt=attempt, ok=False,
              error=entry["error"][:200])
        feedback = (f"Your previous query failed. Error:\n{entry['error']}\n"
                    f"Previous query:\n{cypher}\nReturn a corrected query.")
    return [], None


def select_seeds(sess, cypher_chain, question, intent, entries, vecs):
    """Hybrid seed selection; returns (seeds, path, cypher_attempts)."""
    attempts = []
    if intent.target_provisions:
        seeds = seed_by_template(sess, intent.target_provisions)
        if seeds:
            return seeds, "template", attempts
    if cypher_chain is not None:
        seeds, _ = seed_by_llm_cypher(sess, cypher_chain, question, intent,
                                      attempts)
        if seeds:
            return seeds, "llm_cypher", attempts
    # Deterministic last resort: nearest covered provisions seed the template.
    query = " ".join([question] + intent.scenario_facts)
    near = snippet_search(entries, vecs, query, k=3)
    seeds = seed_by_template(sess, [iri for iri, _, _ in near])
    return seeds, "vector_fallback", attempts


# ---------------------------------------------------------------------------
# Deterministic expansion + context rendering
# ---------------------------------------------------------------------------
EXPAND = (
    "MATCH (s:Statement:Verified) WHERE s.statement_id IN $seeds "
    "OPTIONAL MATCH (s)-[:REFERS_TO|EXCEPTION_OF|CONFLICTS_WITH]-(n:Statement:Verified) "
    "WITH collect(DISTINCT s) + collect(DISTINCT n) AS nodes "
    "UNWIND nodes AS st WITH DISTINCT st "
    "MATCH (st)-[:SOURCED_FROM]->(p:Provision) "
    "RETURN st { .* } AS props, p.iri AS prov_iri, p.text AS prov_text "
    "ORDER BY props.statement_id")

EDGES = (
    "MATCH (a:Statement)-[r:REFERS_TO|EXCEPTION_OF|CONFLICTS_WITH|"
    "SPECIALISES|REDUNDANT_WITH]->(b:Statement) "
    "WHERE a.statement_id IN $ids AND b.statement_id IN $ids "
    "RETURN DISTINCT a.statement_id AS src, type(r) AS rel, "
    "b.statement_id AS dst")


def expand(sess, seeds):
    stmts = [dict(r["props"], prov_iri=r["prov_iri"], prov_text=r["prov_text"])
             for r in sess.run(EXPAND, seeds=seeds)]
    ids = [s["statement_id"] for s in stmts]
    edges = [(r["src"], r["rel"], r["dst"]) for r in sess.run(EDGES, ids=ids)]
    return stmts, edges


def render_statement(props):
    """One context block per statement. The anchor is the reliable pointer to
    the source sentence; the slot rendering (cycle_consistency's deterministic
    template) is a structured gloss, never a substitute for it."""
    sid, cls = props["statement_id"], props["statement_class"]
    lines = [f"[{sid}] {cls}", f"  source: {props.get('source_article')}"]
    if cls == "DEONTIC":
        pseudo = {
            "modality": props.get("modality"),
            "subject": [{"value": v} for v in props.get("subject_text") or []],
            "predicate": [{"value": v} for v in props.get("predicate_text") or []],
            "object": [{"value": v} for v in props.get("object_text") or []],
            "condition": {"value": "; ".join(props.get("condition_text") or [])},
        }
        lines += [f"  modality: {props.get('modality')}",
                  f"  structured: {cc.serialize(pseudo)}"]
    elif cls == "DEFINITIONAL":
        lines.append(f"  defines '{props.get('term')}' as: "
                     f"{props.get('definition_value')}")
    elif cls == "APPLICABILITY":
        lines.append(f"  scope: {props.get('scope_type')} "
                     f"polarity={props.get('polarity')} "
                     f"applies_to: {props.get('applies_to_value')}")
    if props.get("anchor"):
        lines.append(f"  source sentence: \"{props['anchor']}\"")
    return "\n".join(lines)


def build_context(stmts, edges, snippets):
    stmt_txt = "\n\n".join(render_statement(s) for s in stmts) or "(none)"
    edge_txt = "\n".join(f"{a} {rel} {b}" for a, rel, b in edges) or "(none)"
    snip_txt = "\n\n".join(f"[{iri}] {text}" for iri, text, _ in snippets) \
        or "(none)"
    return stmt_txt, edge_txt, snip_txt


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def answer(question, query_id, sess, chains, entries, vecs, topk):
    intent_chain, cypher_chain, synth_chain = chains
    intent = em._retry_invoke(intent_chain, {
        "question": question,
        "covered": "\n".join(f"- {iri}" for iri, _ in entries),
    }, label="intent classification")
    audit("graphrag_query", "intent_classified", query_id=query_id,
          mode=intent.mode, targets=intent.target_provisions,
          n_facts=len(intent.scenario_facts))

    seeds, path, attempts = select_seeds(sess, cypher_chain, question, intent,
                                         entries, vecs)
    audit("graphrag_query", "seeds_selected", query_id=query_id, path=path,
          n_seeds=len(seeds), cypher_attempts=len(attempts))

    stmts, edges = expand(sess, seeds)
    query_text = " ".join([question] + intent.scenario_facts)
    snippets = snippet_search(entries, vecs, query_text, k=topk)
    audit("graphrag_query", "retrieved", query_id=query_id,
          n_statements=len(stmts), n_edges=len(edges),
          snippet_iris=[iri for iri, _, _ in snippets])

    stmt_txt, edge_txt, snip_txt = build_context(stmts, edges, snippets)
    verdict = em._retry_invoke(synth_chain, {
        "question": question, "mode": intent.mode,
        "facts": "\n".join(f"- {f}" for f in intent.scenario_facts) or "(none)",
        "statements": stmt_txt, "edges": edge_txt, "snippets": snip_txt,
    }, label="verdict synthesis")
    audit("graphrag_query", "verdict", query_id=query_id,
          verdict=verdict.verdict, n_cited=len(verdict.cited))

    result = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "query_id": query_id,
        "question": question,
        "intent": intent.model_dump(),
        "seed_path": path,
        "cypher_attempts": attempts,
        "retrieved_statement_ids": [s["statement_id"] for s in stmts],
        "retrieved_edges": [list(e) for e in edges],
        "snippets": [{"iri": iri, "score": round(sc, 4)}
                     for iri, _, sc in snippets],
        "verdict": verdict.model_dump(),
    }
    if verdict.verdict == "INSUFFICIENT":
        with open(REVIEW_QUEUE, "a") as f:
            f.write(json.dumps(result) + "\n")
        audit("graphrag_query", "routed_to_review", query_id=query_id)
    return result


def print_result(res):
    v = res["verdict"]
    print(f"\n=== {res['query_id']}: {v['verdict']}  "
          f"(seed path: {res['seed_path']}, "
          f"{len(res['retrieved_statement_ids'])} statements retrieved)")
    print(f"  Q: {res['question']}")
    print(f"  explanation: {v['explanation']}")
    if v.get("missing_information"):
        print(f"  missing information: {v['missing_information']}")
    print(f"  cited: {', '.join(v['cited']) or '(none)'}")


# ---------------------------------------------------------------------------
# --check: structural self-check, no LLM calls
# ---------------------------------------------------------------------------
def check(sess):
    n = sess.run("MATCH (s:Statement:Verified) RETURN count(s) AS n").single()["n"]
    provisions = covered_provisions(sess)
    with_text = [p for p in provisions if p[1]]
    print(f"graph: {n} :Verified statements, {len(provisions)} covered "
          f"provisions ({len(with_text)} with text)")
    assert n > 0 and with_text, "graph not ready"

    for name, q, params in (
            ("seed_by_prefix", SEED_BY_PREFIX, {"prefixes": ["gdpr:art_5"]}),
            ("expand", EXPAND, {"seeds": []}),
            ("edges", EDGES, {"ids": []})):
        sess.run("EXPLAIN " + q, **params).consume()
        print(f"template {name}: EXPLAIN ok")

    seeds = seed_by_template(sess, ["gdpr:art_5/par_1/pt_e", "aiact:art_12"])
    stmts, edges = expand(sess, seeds)
    conflict = [e for e in edges if e[1] == "CONFLICTS_WITH"]
    print(f"conflict-pair probe: {len(seeds)} seeds -> {len(stmts)} statements, "
          f"{len(edges)} edges (CONFLICTS_WITH: {len(conflict)})")
    assert conflict, "expected the Art 12 / Art 5(1)(e) CONFLICTS_WITH edge"
    print("sample rendering:\n" + render_statement(stmts[0]))

    entries, vecs = load_snippet_index(provisions)
    hits = snippet_search(entries, vecs,
                          "retain operational logs for 90 days", k=3)
    print(f"snippet index: {len(entries)} entries; top-3 for logging probe: "
          + ", ".join(f"{iri} ({s:.2f})" for iri, _, s in hits))
    print("check: OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    ap.add_argument("question", nargs="?", help="a single question")
    ap.add_argument("--batch", help="JSONL of {query_id, question}")
    ap.add_argument("--check", action="store_true",
                    help="structural self-check, no LLM calls")
    ap.add_argument("--backend", default="gemini",
                    choices=["gemini", "mistral"])
    ap.add_argument("--topk", type=int, default=TOPK_SNIPPETS)
    ap.add_argument("--out", default=RESULTS_LOG,
                    help="append results JSONL here")
    args = ap.parse_args()

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as sess:
        if args.check:
            check(sess)
            return
        if not args.question and not args.batch:
            ap.error("provide a question, --batch, or --check")

        chains = build_query_chains(args.backend)
        provisions = covered_provisions(sess)
        entries, vecs = load_snippet_index(provisions)

        if args.batch:
            queries = [json.loads(l) for l in open(args.batch)]
        else:
            queries = [{"query_id": "adhoc", "question": args.question}]

        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        for q in queries:
            res = answer(q["question"], q.get("query_id", "adhoc"), sess,
                         chains, entries, vecs, args.topk)
            with open(args.out, "a") as f:
                f.write(json.dumps(res) + "\n")
            print_result(res)
    driver.close()


if __name__ == "__main__":
    main()
