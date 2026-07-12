"""Step 2 (apply): map the DEONTIC subject field to canonical role IRIs.

For each DEONTIC record, every `subject` element gets a canonical role IRI
written onto it via the deterministic lexicon (mapping/subject_lexicon.json).
A subject with no lexicon hit -- or a hit for the wrong regulation side -- is
FLAGGED for HITL (needs_review=True + a marker), never force-fit to a role.
This mirrors the pipeline's drop-vs-flag discipline.

Only the `subject` field is mapped here; modality (1:1 to Echenim's relations)
and source_article/references (already canonical IRIs) are handled elsewhere.

Output: for each input run dir, a parallel dir <name>_subjmap/ containing
<basename>.subject_mapped.jsonl (gitignored, like other run outputs).

Usage:
  python map_subject.py data/dev_5run_deontic_pred data/holdout_5run_redundant_neg
  python map_subject.py --suffix _subjmap --lexicon mapping/subject_lexicon.json <run_dir> ...
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

from subject_mapping import LEXICON_PATH, load_lexicon, regulation_of, resolve

UNMAPPED_MARKER = "subject_unmapped"
MISMATCH_MARKER = "subject_regulation_mismatch"


def map_record(rec, index, stats):
    """Mutate one record in place; return list of flag markers added (if any)."""
    if rec.get("statement_class") != "DEONTIC":
        return []
    st = rec.get("statement") or {}
    reg = regulation_of(st.get("source_article"))
    markers = []
    for s in (st.get("subject") or []):
        iri, status = resolve(s.get("value"), reg, index)
        s["iri"] = iri  # None when unmapped/mismatch
        stats[status] += 1
        if status == "unmapped" and UNMAPPED_MARKER not in markers:
            markers.append(UNMAPPED_MARKER)
        elif status == "mismatch" and MISMATCH_MARKER not in markers:
            markers.append(MISMATCH_MARKER)
    if markers:
        rec["needs_review"] = True
        existing = rec.get("review_flags") or []
        rec["review_flags"] = existing + [m for m in markers if m not in existing]
    return markers


def process_file(in_path, out_path, index, stats):
    n = 0
    with open(in_path) as fin, open(out_path, "w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            map_record(rec, index, stats)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="run dirs of *.extracted.jsonl")
    ap.add_argument("--lexicon", default=LEXICON_PATH)
    ap.add_argument("--suffix", default="_subjmap",
                    help="parallel output dir suffix (default: _subjmap)")
    args = ap.parse_args()

    _, index = load_lexicon(args.lexicon)
    stats = Counter()
    total_files = 0

    for p in args.paths:
        if os.path.isdir(p):
            files = sorted(glob.glob(os.path.join(p, "*.extracted.jsonl")))
            if not files:
                print(f"  (skip {p}: no *.extracted.jsonl)")
                continue
            out_dir = p.rstrip("/") + args.suffix
        elif p.endswith(".extracted.jsonl") and os.path.isfile(p):
            files = [p]
            out_dir = os.path.dirname(p) or "."
        else:
            sys.exit(f"expected a run directory or *.extracted.jsonl file, got: {p}")
        os.makedirs(out_dir, exist_ok=True)
        for f in files:
            base = os.path.basename(f).replace(".extracted.jsonl", "")
            out_path = os.path.join(out_dir, base + ".subject_mapped.jsonl")
            n = process_file(f, out_path, index, stats)
            total_files += 1
            print(f"  {f} -> {out_path}  ({n} records)")

    total_subj = sum(stats.values())
    print(f"\nfiles written: {total_files}")
    print(f"subject elements: {total_subj}  "
          f"mapped={stats['mapped']}  unmapped={stats['unmapped']}  "
          f"mismatch={stats['mismatch']}")
    if stats["unmapped"] or stats["mismatch"]:
        print("NOTE: flagged records carry needs_review=True + a review_flags marker.")


if __name__ == "__main__":
    main()
