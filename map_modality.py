"""Step (apply): map the DEONTIC modality enum to its deontic relation IRI
(the four-relation scheme is Echenim's; the IRIs are minted under the project's
own `mp:` namespace, see mapping/modality_map.json).

1:1 enum -> relation IRI, deterministic, no LLM. The map lives in
mapping/modality_map.json. This pass CHAINS on the subject-mapping output
(*.subject_mapped.jsonl) so the mapped record accumulates both enrichments.

Field shape mirrors the subject field: the modality field becomes an object
  "modality": {"value": "<ENUM>", "iri": "mp:has..."}
i.e. the original enum string moves to modality.value and the relation IRI is
added as modality.iri. A modality not present in the map -> needs_review=True +
a review_flags marker (flag, not force-fit; schema-drift safety).

Output: for each input dir, a parallel dir <name>_modmap/ containing
<basename>.modality_mapped.jsonl.

Usage:
  python map_modality.py data/dev_5run_deontic_pred_subjmap data/holdout_5run_redundant_neg_subjmap
  python map_modality.py --suffix _modmap --map mapping/modality_map.json <subjmap_dir> ...
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

MAP_PATH = os.path.join(os.path.dirname(__file__), "mapping", "modality_map.json")
UNMAPPED_MARKER = "modality_unmapped"


def load_map(path=MAP_PATH):
    with open(path) as fh:
        m = json.load(fh)
    return m.get("modality_relations", {})


def current_value(modality):
    """Read the enum string whether modality is still a bare string or already
    the {value, iri} object (idempotent re-runs)."""
    if isinstance(modality, dict):
        return modality.get("value")
    return modality


def map_record(rec, relations, stats):
    if rec.get("statement_class") != "DEONTIC":
        return None
    st = rec.get("statement") or {}
    val = current_value(st.get("modality"))
    iri = relations.get(val)
    st["modality"] = {"value": val, "iri": iri}  # None when unmapped
    if iri is None:
        stats["unmapped"] += 1
        rec["needs_review"] = True
        flags = rec.get("review_flags") or []
        if UNMAPPED_MARKER not in flags:
            rec["review_flags"] = flags + [UNMAPPED_MARKER]
        return "unmapped"
    stats["mapped"] += 1
    return "mapped"


def process_file(in_path, out_path, relations, stats):
    n = 0
    with open(in_path) as fin, open(out_path, "w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            map_record(rec, relations, stats)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+",
                    help="subject-mapped run dirs (of *.subject_mapped.jsonl)")
    ap.add_argument("--map", default=MAP_PATH)
    ap.add_argument("--suffix", default="_modmap")
    args = ap.parse_args()

    relations = load_map(args.map)
    stats = Counter()
    total_files = 0

    for p in args.paths:
        if not os.path.isdir(p):
            sys.exit(f"expected a run directory, got: {p}")
        files = sorted(glob.glob(os.path.join(p, "*.subject_mapped.jsonl")))
        if not files:
            print(f"  (skip {p}: no *.subject_mapped.jsonl)")
            continue
        out_dir = p.rstrip("/") + args.suffix
        os.makedirs(out_dir, exist_ok=True)
        for f in files:
            base = os.path.basename(f).replace(".subject_mapped.jsonl", "")
            out_path = os.path.join(out_dir, base + ".modality_mapped.jsonl")
            n = process_file(f, out_path, relations, stats)
            total_files += 1
            print(f"  {f} -> {out_path}  ({n} records)")

    total = stats["mapped"] + stats["unmapped"]
    print(f"\nfiles written: {total_files}")
    print(f"deontic modalities: {total}  mapped={stats['mapped']}  unmapped={stats['unmapped']}")
    if stats["unmapped"]:
        print("NOTE: flagged records carry needs_review=True + review_flags 'modality_unmapped'.")


if __name__ == "__main__":
    main()
