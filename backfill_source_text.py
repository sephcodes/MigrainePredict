"""One-time backfill: add paragraph_text / parent_texts to existing run files
(extract_min.py now emits them; frozen runs predate that). Joins each record's
paragraph_iri against the corpus files and rewrites the run file in place.

Usage:
  python backfill_source_text.py data/dev_5run_deontic_pred* \
      data/holdout_5run_redundant_neg* data/conflict_pair_run*

Records whose IRI is not in the corpus are left untouched (counted, reported).
Already-backfilled records are skipped, so re-running is a no-op.
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(__file__)
CORPUS = [os.path.join(HERE, "data", "gdpr.postscreened.jsonl"),
          os.path.join(HERE, "data", "aiact.postscreened.jsonl")]


def corpus_lookup():
    look = {}
    for path in CORPUS:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            parents = [p["text"] for p in (r.get("parent") or [])
                       if isinstance(p, dict) and p.get("text")]
            look[r["iri"]] = (r.get("text"), parents)
    return look


def main():
    paths = sys.argv[1:]
    if not paths:
        sys.exit(__doc__)
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, "*.jsonl"))) if os.path.isdir(p) else [p]
    look = corpus_lookup()

    total = filled = missing = had = 0
    for f in files:
        recs, changed = [], False
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            total += 1
            if r.get("paragraph_text"):
                had += 1
            else:
                hit = look.get(r.get("paragraph_iri"))
                if hit is None:
                    missing += 1
                else:
                    r["paragraph_text"] = hit[0]
                    if hit[1]:
                        r["parent_texts"] = hit[1]
                    filled += 1
                    changed = True
            recs.append(r)
        if changed:
            with open(f, "w") as out:
                for r in recs:
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"  backfilled {f}")

    print(f"\nrecords: {total}  filled: {filled}  already had: {had}  "
          f"iri not in corpus: {missing}")
    if missing:
        print("WARNING: some records could not be joined to corpus text")


if __name__ == "__main__":
    main()
