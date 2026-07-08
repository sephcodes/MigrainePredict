#!/usr/bin/env python3
"""
score_query.py -- Phase 2 evaluation harness: grade a GraphRAG results file
against the gold query set (report section 5.5 metrics).

Offline metrics (no API, default):
  - completeness: every gold prompt answered, none empty
  - adherence rate: verdict == gold verdict (overall / base / paraphrase)
  - per-label precision, recall, F1 and F2 (beta=2, recall-weighted, Chung)
    + macro-F1 + confusion matrix
  - citation recall/precision: system `cited` vs gold_cited statement ids
    (rows whose gold_cited is empty -- the pure NOT_APPLICABLE probes -- are
    excluded from citation scoring)
  - paraphrase sensitivity: (a) trio consistency -- base question and its two
    rewordings get the same verdict; (b) adherence on the base-10 /
    variant-a / variant-b subsets, reported with the max-min range (the
    Chung F1-range diagnostic)
  - self-correction loop stats (the Echenim 18%->0% comparison)
  - miss list (gold -> system, seed path) for failure-mode attribution

Live metrics (--live; Gemini judge + bge cosine, ~2 calls per query;
implemented directly from the RAGAS formulas, Es et al. 2024 -- no ragas
dependency):
  - faithfulness: the judge lists the factual claims in the explanation and
    marks each supported/unsupported. Claims restating the question's
    scenario count as supported (the scenario is a legitimate source);
    LEGAL claims are judged strictly against the retrieval context, rebuilt
    deterministically from the graph + snippet index as the synthesis saw
    it; score = supported / total claims. Q25-class leaps (plausible but
    ungrounded legal steps) are what this isolates.
  - answer relevance: the judge writes 3 questions the explanation answers;
    score = mean bge cosine between each and the original question.

Usage:
  python score_query.py                                   # gold_run1, offline
  python score_query.py data/graphrag/RESULTS.jsonl --live
  python score_query.py RESULTS --json data/graphrag/OUT.metrics.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict

DEFAULT_RESULTS = "data/graphrag/gold_run1.results.jsonl"
DEFAULT_GOLD = "data/graphrag/gold_queries.json"
LABELS = ["COMPLIANT", "NON_COMPLIANT", "INSUFFICIENT", "NOT_APPLICABLE"]


def load_gold(path):
    g = json.load(open(path))
    gold = {q["query_id"]: q for q in g["queries"]}
    para_of = {p["query_id"]: p["paraphrase_of"] for p in g["paraphrases"]}
    return gold, para_of


def load_results(path):
    results = {}
    for line in open(path):
        r = json.loads(line)
        results[r["query_id"]] = r  # last wins if rerun
    return results


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    f2 = 5 * p * r / (4 * p + r) if 4 * p + r else 0.0
    return p, r, f1, f2


def verdict_metrics(results, gold, para_of):
    gv = lambda q: gold[para_of.get(q, q)]["gold_verdict"]
    conf = Counter()
    hits = defaultdict(lambda: [0, 0])   # subset -> [agree, n]
    misses = []
    for qid, r in sorted(results.items()):
        s, g = r["verdict"]["verdict"], gv(qid)
        conf[(g, s)] += 1
        subset = "paraphrase" if qid in para_of else "base"
        hits[subset][1] += 1
        hits[subset][0] += s == g
        if s != g:
            misses.append((qid, g, s, r["seed_path"]))
    per_label = {}
    for lab in LABELS:
        tp = conf.get((lab, lab), 0)
        fp = sum(conf.get((g, lab), 0) for g in LABELS if g != lab)
        fn = sum(conf.get((lab, s), 0) for s in LABELS if s != lab)
        per_label[lab] = prf(tp, fp, fn)
    macro_f1 = sum(v[2] for v in per_label.values()) / len(LABELS)
    return conf, hits, misses, per_label, macro_f1


def citation_metrics(results, gold, para_of):
    rec_n = rec_d = prec_n = prec_d = 0
    for qid, r in results.items():
        gc = set(gold[para_of.get(qid, qid)]["gold_cited"])
        if not gc:
            continue
        sc = set(r["verdict"]["cited"])
        rec_n += len(gc & sc)
        rec_d += len(gc)
        prec_n += len(sc & gc)
        prec_d += len(sc)
    return (rec_n / rec_d if rec_d else 0.0,
            prec_n / prec_d if prec_d else 0.0, rec_d, prec_d)


def paraphrase_metrics(results, gold, para_of):
    gv = lambda q: gold[para_of.get(q, q)]["gold_verdict"]
    trios, consistent = [], 0
    subset_hits = {"base10": [0, 0], "variant_a": [0, 0], "variant_b": [0, 0]}
    for base in sorted(set(para_of.values())):
        variants = sorted(q for q, b in para_of.items() if b == base)
        trio = [base] + variants
        verdicts = {q: results[q]["verdict"]["verdict"]
                    for q in trio if q in results}
        trios.append((base, verdicts))
        consistent += len(set(verdicts.values())) == 1
        for key, q in zip(("base10", "variant_a", "variant_b"), trio):
            if q in results:
                subset_hits[key][1] += 1
                subset_hits[key][0] += results[q]["verdict"]["verdict"] == gv(q)
    rates = {k: (a / n if n else 0.0) for k, (a, n) in subset_hits.items()}
    return trios, consistent, rates, max(rates.values()) - min(rates.values())


def loop_stats(results):
    paths = Counter(r["seed_path"] for r in results.values())
    attempts = [a for r in results.values()
                for a in r.get("cypher_attempts", [])]
    by_attempt = Counter((a["attempt"], bool(a["error"])) for a in attempts)
    n_queries = sum(1 for r in results.values() if r.get("cypher_attempts"))
    return paths, attempts, by_attempt, n_queries


# ---------------------------------------------------------------------------
# Live metrics (RAGAS-style faithfulness + answer relevance, Es et al. 2024)
# ---------------------------------------------------------------------------
def rebuild_context(r, sess, entries_by_iri):
    """Reconstruct the synthesis context as the run saw it: rendered
    statements + edges from the graph (empty for baseline runs) and snippet
    texts from the cached index."""
    import graphrag_query as gq
    ids = r.get("retrieved_statement_ids") or []
    parts = []
    if ids and sess is not None:
        rows = sess.run(
            "MATCH (st:Statement) WHERE st.statement_id IN $ids "
            "RETURN st { .* } AS props ORDER BY props.statement_id", ids=ids)
        parts += [gq.render_statement(dict(x["props"])) for x in rows]
        for a, rel, b in r.get("retrieved_edges") or []:
            parts.append(f"{a} {rel} {b}")
    for s in r.get("snippets") or []:
        text = entries_by_iri.get(s["iri"])
        if text:
            parts.append(f"[{s['iri']}] {text}")
    return "\n\n".join(parts)


def run_live(results, backend):
    from langchain_core.prompts import ChatPromptTemplate
    from neo4j import GraphDatabase
    from pydantic import BaseModel, Field
    import extract_min as em
    import graphrag_query as gq

    class JudgedClaim(BaseModel):
        claim: str = Field(description="One factual claim made by the answer.")
        supported: bool = Field(description="True only if the context states "
                                            "or directly entails the claim.")

    class FaithfulnessJudgement(BaseModel):
        claims: list[JudgedClaim]

    class GeneratedQuestions(BaseModel):
        questions: list[str] = Field(
            description="Exactly 3 questions this answer would answer.")

    llm = em._llm(backend)
    faith_chain = ChatPromptTemplate.from_messages([
        ("system",
         "Decompose the answer into its individual factual claims. For EACH "
         "claim decide whether it is supported:\n"
         "- A claim that merely restates or paraphrases facts given in the "
         "QUESTION is supported (the scenario is a legitimate source).\n"
         "- A legal or regulatory claim (a rule asserted, a duty, an "
         "exception, a definition) is supported ONLY if the CONTEXT states "
         "it or directly entails it. Mark supported=false for legal content "
         "the context does not contain, however plausible or legally "
         "correct it may be.\n"
         "- A conclusion is supported if it follows from supported claims "
         "alone."),
        ("user", "Question:\n{question}\n\nContext:\n{context}\n\n"
                 "Answer:\n{answer}"),
    ]) | llm.with_structured_output(FaithfulnessJudgement)
    relevance_chain = ChatPromptTemplate.from_messages([
        ("system",
         "Write exactly 3 standalone questions to which the given answer "
         "would be a good answer. Do not copy the answer's wording."),
        ("user", "Answer:\n{answer}"),
    ]) | llm.with_structured_output(GeneratedQuestions)

    driver = GraphDatabase.driver(gq.NEO4J_URI,
                                  auth=(gq.NEO4J_USER, gq.NEO4J_PASSWORD))
    meta = json.load(open(gq.INDEX_META))
    entries_by_iri = {iri: text for iri, text in meta["entries"]}

    rows, failed = [], []
    with driver.session() as sess:
        for qid, r in sorted(results.items()):
            # One flaky query must not lose the whole pass (a mid-run Gemini
            # disconnect killed 48 completed queries once).
            try:
                answer = r["verdict"]["explanation"]
                context = rebuild_context(r, sess, entries_by_iri)
                fj = em._retry_invoke(faith_chain,
                                      {"question": r["question"],
                                       "context": context, "answer": answer},
                                      label=f"faithfulness {qid}")
                n_sup = sum(c.supported for c in fj.claims)
                f_score = n_sup / len(fj.claims) if fj.claims else 1.0

                gen = em._retry_invoke(relevance_chain, {"answer": answer},
                                       label=f"relevance {qid}")
                embs = gq._embedder().encode([r["question"]] + gen.questions,
                                             normalize_embeddings=True)
                rel = (float((embs[1:] @ embs[0]).mean())
                       if gen.questions else 0.0)
            except Exception as e:
                failed.append(qid)
                print(f"  live {qid}: FAILED ({str(e)[:120]})")
                continue
            rows.append({"query_id": qid, "faithfulness": f_score,
                         "answer_relevance": rel, "n_claims": len(fj.claims),
                         "unsupported_claims": [c.claim for c in fj.claims
                                                if not c.supported]})
            print(f"  live {qid}: faithfulness {f_score:.2f} "
                  f"({n_sup}/{len(fj.claims)}), relevance {rel:.2f}")
    driver.close()
    if failed:
        print(f"  live pass incomplete: {len(failed)} queries failed "
              f"({failed}) -- re-run --live to retry")
    return rows


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="?", default=DEFAULT_RESULTS)
    ap.add_argument("--gold", default=DEFAULT_GOLD)
    ap.add_argument("--live", action="store_true",
                    help="also run faithfulness + answer relevance (Gemini)")
    ap.add_argument("--backend", default="gemini")
    ap.add_argument("--json", help="write all metrics to this JSON file")
    args = ap.parse_args()

    gold, para_of = load_gold(args.gold)
    results = load_results(args.results)

    expected = set(gold) | set(para_of)
    missing = sorted(expected - set(results))
    extra = sorted(set(results) - expected)
    empty = [q for q, r in results.items() if not r["verdict"].get("verdict")]
    print(f"== {args.results}")
    print(f"completeness: {len([q for q in results if q in expected])}"
          f"/{len(expected)}"
          + (f"  MISSING {missing}" if missing else "")
          + (f"  EXTRA(ignored) {extra}" if extra else "")
          + (f"  EMPTY {empty}" if empty else ""))
    results = {q: r for q, r in results.items() if q in expected}

    conf, hits, misses, per_label, macro_f1 = verdict_metrics(
        results, gold, para_of)
    total_a = sum(a for a, _ in hits.values())
    total_n = sum(n for _, n in hits.values())
    print(f"\nadherence rate: {total_a}/{total_n} = {total_a/total_n:.3f}"
          + "".join(f"  ({k} {a}/{n})" for k, (a, n) in sorted(hits.items())))

    print(f"\n{'label':<16}{'P':>7}{'R':>7}{'F1':>7}{'F2':>7}")
    for lab in LABELS:
        p, r, f1, f2 = per_label[lab]
        print(f"{lab:<16}{p:>7.3f}{r:>7.3f}{f1:>7.3f}{f2:>7.3f}")
    print(f"macro-F1: {macro_f1:.3f}")

    print("\nconfusion (rows=gold, cols=system):")
    print("  " + "".join(f"{l[:6]:>8}" for l in LABELS))
    for g in LABELS:
        print(f"  {g[:6]:<6}"
              + "".join(f"{conf.get((g, s), 0):>8}" for s in LABELS))

    c_rec, c_prec, n_gold_c, n_sys_c = citation_metrics(results, gold, para_of)
    print(f"\ncitations: recall {c_rec:.3f} ({n_gold_c} gold ids), "
          f"precision {c_prec:.3f} ({n_sys_c} system ids)")

    trios, consistent, rates, spread = paraphrase_metrics(
        results, gold, para_of)
    print(f"\nparaphrase sensitivity: {consistent}/{len(trios)} trios "
          f"verdict-consistent; subset adherence "
          + " ".join(f"{k}={v:.3f}" for k, v in sorted(rates.items()))
          + f"; range {spread:.3f}")
    for base, vs in trios:
        if len(set(vs.values())) > 1:
            print(f"  inconsistent {base}: {vs}")

    paths, attempts, by_attempt, n_loop = loop_stats(results)
    print(f"\nseed paths: {dict(paths)}")
    if attempts:
        errs = sum(1 for a in attempts if a["error"])
        print(f"self-correction loop: {n_loop} queries, {len(attempts)} "
              f"attempts, {errs} errors fed back; per attempt#: "
              + ", ".join(f"{k[0]}:{'err' if k[1] else 'ok'}x{v}"
                          for k, v in sorted(by_attempt.items())))

    if misses:
        print("\nmisses (for failure-mode attribution):")
        for qid, g, s, path in misses:
            print(f"  {qid:6} {g:14} -> {s:14} [{path}]")

    out = {"results_file": args.results,
           "adherence": {k: {"agree": a, "n": n}
                         for k, (a, n) in hits.items()},
           "per_label": {l: dict(zip(("P", "R", "F1", "F2"), v))
                         for l, v in per_label.items()},
           "macro_f1": macro_f1,
           "citation": {"recall": c_rec, "precision": c_prec},
           "paraphrase": {"consistent_trios": consistent,
                          "n_trios": len(trios),
                          "subset_adherence": rates, "range": spread},
           "seed_paths": dict(paths),
           "misses": [list(m) for m in misses]}

    if args.live:
        print("\nrunning live metrics (faithfulness + answer relevance)...")
        live_rows = run_live(results, args.backend)
        fs = [x["faithfulness"] for x in live_rows]
        rs = [x["answer_relevance"] for x in live_rows]
        print(f"\nfaithfulness: mean {sum(fs)/len(fs):.3f} min {min(fs):.3f}")
        print(f"answer relevance: mean {sum(rs)/len(rs):.3f} "
              f"min {min(rs):.3f}")
        unsupported = [(x["query_id"], c) for x in live_rows
                       for c in x["unsupported_claims"]]
        if unsupported:
            print(f"unsupported claims ({len(unsupported)}):")
            for qid, claim in unsupported[:15]:
                print(f"  [{qid}] {claim[:110]}")
        out["live"] = live_rows

    if args.json:
        json.dump(out, open(args.json, "w"), indent=2)
        print(f"\nmetrics written to {args.json}")


if __name__ == "__main__":
    main()
