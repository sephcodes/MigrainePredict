#!/usr/bin/env python3
"""PROVISION-LEVEL citation recall/precision for the cross-system comparison in
graphrag_stage_summary.md sec 12.15, plus the citation-tightening variant.

IMPORTANT — this is NOT the headline citation metric. The KG pipeline's official
citation numbers are the STATEMENT-level figures from score_query.py (already in
the *.metrics.json files): clean-render 0.595/0.246, pre-fix 0.811/0.265.

This script exists ONLY to give the bare LLM and the vector baseline a
comparable number: they cite provision-level IRIs (gdpr:art_9/par_1, no #sN), so
score_query's statement-level match reads 0.000 for them as a pure granularity
artifact. Stripping #sN on both sides fixes that — but PROVISION-level matching
is COARSER (any statement of the right article counts), so it reads HIGHER than
the statement-level headline (e.g. KG clean-render 0.628 here vs 0.595 official).
Never mix the two levels.

Usage:  python citation_analysis.py            # writes data/graphrag/citation_analysis.json
"""
import json, re, os

GR = "data/graphrag"
GOLD = f"{GR}/gold_queries.json"
OUT = f"{GR}/citation_analysis.json"

RUNS = {
    "bare_llm_gemini":            "llmonly_gemini.results.jsonl",
    "kg_pipeline_prefix_gemini":  "gold_corpus_gemini.results.jsonl",
    "kg_pipeline_cleanrender_gemini": "gold_corpus_gemini_cleanrender.results.jsonl",
    "vector_rag_baseline_gemini": "baseline_corpus.results.jsonl",
}


def prov(iri):
    return re.sub(r"#s\d+$", "", iri)


def main():
    J = json.load(open(GOLD))
    gold = {q["query_id"]: q for q in J["queries"]}
    para = {p["query_id"]: p["paraphrase_of"] for p in J.get("paraphrases", [])}

    def goldprov(qid):
        base = gold.get(para.get(qid, qid), {})
        return set(prov(c) for c in (base.get("gold_cited") or []))

    def score(rows, tighten=False):
        tp = fp = fn = 0
        for r in rows:
            g = goldprov(r["query_id"])
            v = r["verdict"]
            cited = set(prov(c) for c in (v.get("cited") or [])
                        if "CONFLICTS" not in c)
            if tighten:  # keep only citations named in the explanation prose
                expl = v.get("explanation") or ""
                cited = {c for c in cited if c in expl}
            tp += len(g & cited); fp += len(cited - g); fn += len(g - cited)
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        return {"recall": round(rec, 3), "precision": round(prec, 3),
                "tp": tp, "fp": fp, "fn": fn}

    out = {"note": "provision-level (statement #sN suffix stripped both sides)",
           "runs": {}}
    for name, fn in RUNS.items():
        path = f"{GR}/{fn}"
        if not os.path.exists(path):
            out["runs"][name] = "MISSING"; continue
        rows = [json.loads(l) for l in open(path)]
        out["runs"][name] = score(rows)

    # citation tightening, on the clean-render run (sec 12.15)
    cr = f"{GR}/gold_corpus_gemini_cleanrender.results.jsonl"
    if os.path.exists(cr):
        rows = [json.loads(l) for l in open(cr)]
        out["cleanrender_tightening"] = {
            "all_cited": score(rows, tighten=False),
            "named_in_explanation_only": score(rows, tighten=True)}

    out["WARNING"] = ("PROVISION-level (coarser) — reads higher than the "
                      "statement-level headline. KG headline = score_query.py "
                      "statement-level: clean-render 0.595/0.246, pre-fix "
                      "0.811/0.265. Use this only for the bare-LLM/baseline row.")
    json.dump(out, open(OUT, "w"), indent=2)
    print(f"wrote {OUT}")
    print("*** PROVISION-LEVEL (coarser, reads HIGH); KG headline is "
          "score_query statement-level 0.595/0.246 ***\n")
    print(f"{'run':40s} {'recall':>8} {'precision':>10}")
    for name, m in out["runs"].items():
        if isinstance(m, dict):
            print(f"{name:40s} {m['recall']:>8} {m['precision']:>10}")
    if "cleanrender_tightening" in out:
        t = out["cleanrender_tightening"]
        print(f"\ntightening (clean-render): all={t['all_cited']['recall']}/"
              f"{t['all_cited']['precision']}  "
              f"named-in-expl={t['named_in_explanation_only']['recall']}/"
              f"{t['named_in_explanation_only']['precision']}")


if __name__ == "__main__":
    main()
