"""
Score the LLM adjudicator against your held-out manual decisions.

Ground truth = content_map_reviewed.json (your manually_* rows). Prediction =
content_map.json after adjudicate_content.py has run (llm_suggested_* / escalated
rows). Rows are matched on (slot, regulation, value, source_article). For each
matched pair it reports:
  - disposition agreement (did the LLM choose mapped/literal/flag as you did),
  - IRI exact-match and overlap on the rows you both mapped,
  - an agreement-vs-confidence table (to pick the escalation threshold),
  - the escalation rate.

This is inter-annotator-style agreement against a single expert (you), not
ground-truth correctness — report it as such.

Usage:
    python score_adjudication.py
    python score_adjudication.py --pred mapping/content_map.json --gold mapping/content_map_reviewed.json
"""

import argparse
import json
import os
from collections import Counter

HERE = os.path.dirname(__file__)


def _disposition(row):
    """Normalise any row to one of mapped/literal/flag/escalated/other."""
    st = row.get("status", "")
    for tag in ("manually_", "llm_suggested_"):
        if st.startswith(tag):
            return st[len(tag):]
    if st == "escalated":
        return "escalated"
    return st  # mapped/literal/review/... (unadjudicated)


def _index(cm):
    d = {}
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            for r in cm.get(slot, {}).get(reg, []):
                d[(slot, reg, r["value"], r.get("source_article"))] = r
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pred", default=os.path.join(HERE, "mapping", "content_map.json"))
    ap.add_argument("--gold", default=os.path.join(HERE, "mapping", "content_map_reviewed.json"))
    args = ap.parse_args(argv)

    P, G = _index(json.load(open(args.pred))), _index(json.load(open(args.gold)))

    def _adjudicated(row):
        st = row.get("status", "")
        return st.startswith("llm_suggested_") or st == "escalated"

    # score ONLY rows the adjudicator actually decided (llm_suggested_* / escalated)
    # against your human answer — auto-mapped/auto-literal rows the adjudicator never
    # touched are excluded so they can't trivially inflate agreement.
    gold_keys = [k for k in G if _disposition(G[k]) in ("mapped", "literal", "flag")]
    pairs = [(k, P[k], G[k]) for k in gold_keys if k in P and _adjudicated(P[k])]
    missing = [k for k in gold_keys if k not in P]
    skipped_not_adjudicated = sum(1 for k in gold_keys if k in P and not _adjudicated(P[k]))

    n = len(pairs)
    if not n:
        print("no matched pairs to score.")
        return

    disp_agree = 0
    escalated = 0
    iri_exact = iri_overlap = mapped_pairs = 0
    conf_buckets = {b: [0, 0] for b in ("0.0-0.5", "0.5-0.7", "0.7-0.9", "0.9-1.0")}  # [agree, total]
    confusion = Counter()  # (gold_disp, pred_disp)

    for k, p, g in pairs:
        gd, pd = _disposition(g), _disposition(p)
        confusion[(gd, pd)] += 1
        if pd == "escalated":
            escalated += 1
            continue
        agree = (gd == pd)
        disp_agree += int(agree)
        if gd == "mapped" and pd == "mapped":
            mapped_pairs += 1
            gi, pi = set(g.get("iri", [])), set(p.get("iri", []))
            iri_exact += int(gi == pi)
            iri_overlap += int(bool(gi & pi))
        c = p.get("llm_confidence")
        if isinstance(c, (int, float)):
            b = ("0.0-0.5" if c < 0.5 else "0.5-0.7" if c < 0.7 else "0.7-0.9" if c < 0.9 else "0.9-1.0")
            conf_buckets[b][1] += 1
            conf_buckets[b][0] += int(agree)

    scored = n - escalated
    print(f"adjudicated pairs scored: {n}   (gold decisions the adjudicator didn't touch, excluded: {skipped_not_adjudicated}; gold rows missing from prediction: {len(missing)})")
    print(f"escalated by adjudicator: {escalated}  ({escalated/n:.0%})")
    print(f"\ndisposition agreement (non-escalated): {disp_agree}/{scored} = {disp_agree/scored:.1%}" if scored else "all escalated")
    if mapped_pairs:
        print(f"  IRI exact-match  (both mapped): {iri_exact}/{mapped_pairs} = {iri_exact/mapped_pairs:.1%}")
        print(f"  IRI any-overlap  (both mapped): {iri_overlap}/{mapped_pairs} = {iri_overlap/mapped_pairs:.1%}")

    print("\nconfusion (gold -> pred):")
    for (gd, pd), c in sorted(confusion.items()):
        mark = "" if gd == pd else "   <-- mismatch"
        print(f"  {gd:9s} -> {pd:12s} {c}{mark}")

    print("\nagreement by confidence bucket (for threshold choice):")
    for b, (a, t) in conf_buckets.items():
        if t:
            print(f"  {b}: {a}/{t} = {a/t:.0%}")
    # suggest the lowest cut where agreement >= 95%
    print("\n(pick the escalation threshold at the bucket where agreement drops below ~95%)")


if __name__ == "__main__":
    main()