"""Step 1 (audit): keep the subject lexicon honest as the data scales.

Scans extraction run dirs, collects the distinct DEONTIC subject surface forms
per regulation side, and reports any form NOT already covered by
mapping/subject_lexicon.json -- i.e. the forms that need an IRI decision (HITL)
before step 2 (map_subject.py) can map them.

This does NOT compute coverage/accuracy metrics and does NOT mutate anything --
it is the table-maintenance step, not the measurement step.

Usage:
  python build_subject_lexicon.py data/dev_5run_deontic_pred data/holdout_5run_redundant_neg
  python build_subject_lexicon.py --lexicon mapping/subject_lexicon.json <run_dir> ...

A run dir is any directory of *.extracted.jsonl files (e.g. the 5-run eval dirs).
Plain .jsonl files may also be passed directly.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

from subject_mapping import LEXICON_PATH, load_lexicon, regulation_of, resolve


def iter_records(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.extracted.jsonl"))))
        else:
            files.append(p)
    if not files:
        sys.exit("no .extracted.jsonl files found in the given paths")
    for f in files:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield f, json.loads(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="run dirs or .jsonl files to audit")
    ap.add_argument("--lexicon", default=LEXICON_PATH)
    args = ap.parse_args()

    _, index = load_lexicon(args.lexicon)

    # (surface, regulation) -> count, and its resolution status
    seen = Counter()
    status_of = {}
    for _f, rec in iter_records(args.paths):
        if rec.get("statement_class") != "DEONTIC":
            continue
        st = rec.get("statement") or {}
        reg = regulation_of(st.get("source_article"))
        for s in (st.get("subject") or []):
            val = s.get("value")
            key = (val, reg)
            seen[key] += 1
            if key not in status_of:
                _iri, status = resolve(val, reg, index)
                status_of[key] = status

    mapped = sorted(k for k, s in status_of.items() if s == "mapped")
    unmapped = sorted(k for k, s in status_of.items() if s == "unmapped")
    mismatch = sorted(k for k, s in status_of.items() if s == "mismatch")

    print(f"lexicon: {args.lexicon}")
    print(f"distinct (surface, regulation) pairs: {len(status_of)}\n")

    print(f"COVERED by lexicon ({len(mapped)}):")
    for val, reg in mapped:
        iri, _ = resolve(val, reg, index)
        print(f"  [{reg}] {val!r:40s} -> {iri}   (x{seen[(val, reg)]})")

    if mismatch:
        print(f"\nWRONG-REGULATION ({len(mismatch)}) -- HITL, do not map:")
        for val, reg in mismatch:
            print(f"  [{reg}] {val!r:40s}   (x{seen[(val, reg)]})")

    print(f"\nUNMAPPED ({len(unmapped)}) -- need an IRI decision before step 2:")
    if not unmapped:
        print("  (none)")
    for val, reg in unmapped:
        print(f"  [{reg}] {val!r:40s}   (x{seen[(val, reg)]})")

    # non-zero exit if anything needs attention, so this can gate a pipeline
    sys.exit(1 if (unmapped or mismatch) else 0)


if __name__ == "__main__":
    main()
