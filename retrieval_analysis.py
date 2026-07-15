#!/usr/bin/env python3
"""Retrieval diagnostics for the query layer: the recall@k curve (dense vs
hybrid) and the per-probe rankings (dense / sparse / hybrid / hybrid+rerank).
Reproduces graphrag_stage_summary.md sec 12.14 from the graph + gold set.

No LLM calls. Uses BGE-M3 (dense + BM25-like sparse via FlagEmbedding) and the
bge-reranker-base cross-encoder. Re-encodes the ~1,620 covered provisions each
run (a few minutes). Writes data/graphrag/retrieval_analysis.json.

Env: set KMP_DUPLICATE_LIB_OK=TRUE on macOS (OpenMP double-load workaround).

Usage:  KMP_DUPLICATE_LIB_OK=TRUE python retrieval_analysis.py
"""
import os, json, re
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
import numpy as np
import graphrag_query as gq
from neo4j import GraphDatabase

GOLD = "data/graphrag/gold_queries.json"
OUT = "data/graphrag/retrieval_analysis.json"
KS = [1, 3, 5, 10, 20, 30, 50, 100]
PROBES = ["retain operational logs for 90 days",
          "explicit consent to process health data for migraine prediction",
          "erase personal data on request",
          "sell health data to advertisers without consent"]


def prov(iri):
    return re.sub(r"#s\d+$", "", iri)


def main():
    J = json.load(open(GOLD))
    gold = {q["query_id"]: q for q in J["queries"]}
    para = {p["query_id"]: p["paraphrase_of"] for p in J.get("paraphrases", [])}
    qs = []
    for q in J["queries"] + J.get("paraphrases", []):
        g = set(prov(c) for c in (gold.get(para.get(q["query_id"], q["query_id"]),
                {}).get("gold_cited") or []))
        if g:
            qs.append((q["question"], g))

    d = GraphDatabase.driver(gq.NEO4J_URI, auth=(gq.NEO4J_USER, gq.NEO4J_PASSWORD))
    with d.session() as s:
        provs = [(i, t) for i, t in gq.covered_provisions(s) if t]
    d.close()
    iris = [p[0] for p in provs]; texts = [p[1] for p in provs]

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    m = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
    P = m.encode(texts, return_dense=True, return_sparse=True,
                 batch_size=16, max_length=512)
    pdense = np.array(P["dense_vecs"]); plex = P["lexical_weights"]
    ce = CrossEncoder("BAAI/bge-reranker-base", max_length=512)

    def dense_rank(q):
        Q = m.encode([q], return_dense=True)
        return list((pdense @ np.array(Q["dense_vecs"][0])).argsort()[::-1])

    def hybrid_rank(q):
        Q = m.encode([q], return_dense=True, return_sparse=True)
        sc = (pdense @ np.array(Q["dense_vecs"][0])
              + 0.5 * np.array([m.compute_lexical_matching_score(
                  Q["lexical_weights"][0], w) for w in plex]))
        return list(sc.argsort()[::-1])

    def recall_curve(rankfn):
        rec = {k: [] for k in KS}
        for question, g in qs:
            ranked = [iris[i] for i in rankfn(question)[:max(KS)]]
            for k in KS:
                rec[k].append(len(g & set(ranked[:k])) / len(g))
        return {k: round(float(np.mean(v)), 3) for k, v in rec.items()}

    out = {"n_queries": len(qs), "ks": KS,
           "recall_at_k": {"dense": recall_curve(dense_rank),
                           "hybrid": recall_curve(hybrid_rank)},
           "probes": {}}

    for pr in PROBES:
        Q = m.encode([pr], return_dense=True, return_sparse=True)
        qd = np.array(Q["dense_vecs"][0]); ql = Q["lexical_weights"][0]
        dense = pdense @ qd
        sparse = np.array([m.compute_lexical_matching_score(ql, w) for w in plex])
        hyb = dense + 0.5 * sparse
        h20 = list(hyb.argsort()[::-1][:20])
        rr = [h20[o] for o in np.argsort(ce.predict([(pr, texts[i]) for i in h20]))[::-1]]
        out["probes"][pr] = {
            "dense_top3": [iris[i] for i in dense.argsort()[::-1][:3]],
            "sparse_top3": [iris[i] for i in sparse.argsort()[::-1][:3]],
            "hybrid_top3": [iris[i] for i in hyb.argsort()[::-1][:3]],
            "hybrid_rerank_top3": [iris[i] for i in rr[:3]]}

    json.dump(out, open(OUT, "w"), indent=2)
    print(f"wrote {OUT}\n")
    print(f"{'k':>4} {'dense':>8} {'hybrid':>8}")
    for k in KS:
        print(f"{k:>4} {out['recall_at_k']['dense'][k]:>8} "
              f"{out['recall_at_k']['hybrid'][k]:>8}")
    print("\nprobes (want: consent->art_9, logging->art_12/art_19):")
    for pr, r in out["probes"].items():
        print(f"  {pr[:44]!r}")
        for mode in ("dense_top3", "sparse_top3", "hybrid_top3", "hybrid_rerank_top3"):
            print(f"    {mode:20s} {', '.join(r[mode])}")


if __name__ == "__main__":
    main()
