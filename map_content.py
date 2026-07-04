"""Step (apply): merge the adjudicated content mapping (predicate / object /
condition) back onto the extraction records, alongside the subject and modality
IRIs already applied.

Chains on the modality-mapped output (*.modality_mapped.jsonl), so the final
record carries subject.iri + modality.{value,iri} + predicate/object/condition
iris in one place, ready for Neo4j load.

Source of decisions: mapping/content_map_reviewed_2.json (the human-adjudicated
worksheet), keyed by (slot, regulation, value, article-root) -- the SAME keys the
matcher used: reg = source_article.split(':')[0], art = source_article.split('/')[0],
predicate value = normalise_predicate(value, modality), object/condition = raw value.

Each predicate/object element (a {value, method} dict) and the condition dict get:
  - iri            : list of vocab IRIs (empty for literal/flag/no_target)
  - mapping_status : the adjudicated disposition (mapped / manually_mapped /
                     llm_suggested_mapped / literal / manually_flag / no_target ...),
                     preserved so auto vs LLM-proposed vs human provenance is visible.
A value with no worksheet row -> iri=[], mapping_status="unmatched", needs_review
(flag, don't guess). The dry-run join is 0 misses, so this is only a guard.

Output: for each input dir, a parallel <name>_content/ of *.content_mapped.jsonl.

Usage:
  python map_content.py data/dev_5run_deontic_pred_subjmap_modmap data/holdout_5run_redundant_neg_subjmap_modmap
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

from predicate_norm import normalise_predicate

HERE = os.path.dirname(__file__)
REVIEWED = os.path.join(HERE, "mapping", "content_map_reviewed_2.json")
UNMATCHED = "unmatched"


def load_decisions(path):
    """-> {(slot, reg, value, art): {"iri": [...], "status": str}}."""
    w = json.load(open(path))
    look = {}
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            for r in w.get(slot, {}).get(reg, []):
                look[(slot, reg, r["value"], r["source_article"])] = {
                    "iri": r.get("iri", []), "status": r.get("status"),
                }
    return look


def modality_value(mod):
    return mod["value"] if isinstance(mod, dict) else mod


def apply_record(rec, look, stats):
    if rec.get("statement_class") != "DEONTIC":
        return False
    st = rec.get("statement") or {}
    src = st.get("source_article") or ""
    reg = src.split(":")[0]
    art = src.split("/")[0]
    modv = modality_value(st.get("modality"))
    flagged = False

    def decide(slot, key_value):
        d = look.get((slot, reg, key_value, art))
        if d is None:
            stats[(slot, UNMATCHED)] += 1
            return [], UNMATCHED
        stats[(slot, d["status"])] += 1
        return d["iri"], d["status"]

    for p in (st.get("predicate") or []):
        nv, _ = normalise_predicate(p.get("value") or "", modv)
        p["iri"], p["mapping_status"] = decide("predicate", nv)
        flagged |= p["mapping_status"] == UNMATCHED
    for o in (st.get("object") or []):
        o["iri"], o["mapping_status"] = decide("object", o.get("value") or "")
        flagged |= o["mapping_status"] == UNMATCHED
    cond = st.get("condition")
    if isinstance(cond, dict) and cond.get("value"):
        cond["iri"], cond["mapping_status"] = decide("condition", cond["value"])
        flagged |= cond["mapping_status"] == UNMATCHED

    if flagged:
        rec["needs_review"] = True
        fl = rec.get("review_flags") or []
        if "content_unmatched" not in fl:
            rec["review_flags"] = fl + ["content_unmatched"]
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="modality-mapped run dirs (*.modality_mapped.jsonl)")
    ap.add_argument("--reviewed", default=REVIEWED)
    ap.add_argument("--suffix", default="_content")
    args = ap.parse_args()

    look = load_decisions(args.reviewed)
    stats = Counter()
    n_files = n_recs = 0

    for p in args.paths:
        if not os.path.isdir(p):
            sys.exit(f"expected a run directory, got: {p}")
        files = sorted(glob.glob(os.path.join(p, "*.modality_mapped.jsonl")))
        if not files:
            print(f"  (skip {p}: no *.modality_mapped.jsonl)")
            continue
        out_dir = p.rstrip("/") + args.suffix
        os.makedirs(out_dir, exist_ok=True)
        for f in files:
            base = os.path.basename(f).replace(".modality_mapped.jsonl", "")
            out_path = os.path.join(out_dir, base + ".content_mapped.jsonl")
            with open(f) as fin, open(out_path, "w") as fout:
                for line in fin:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    apply_record(rec, look, stats)
                    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n_recs += 1
            n_files += 1
            print(f"  {f} -> {out_path}")

    print(f"\nfiles written: {n_files}   records: {n_recs}")
    for slot in ("predicate", "object", "condition"):
        row = {s: c for (sl, s), c in stats.items() if sl == slot}
        print(f"  {slot:10s} {row}")
    unmatched = sum(c for (sl, s), c in stats.items() if s == UNMATCHED)
    if unmatched:
        print(f"  WARNING: {unmatched} unmatched slot values flagged needs_review")


if __name__ == "__main__":
    main()
