#!/usr/bin/env python3
"""
score_verification.py -- detector P/R over the labelled verification worksheet.

Reads verification_reviewed.json (make_verification_worksheet.py output with
human_label filled) and scores detector_verdict against human_label:

  - overall agreement,
  - per-class precision (of the pairs the detector called X, how many the
    human also called X) and recall (of the pairs the human called X, how
    many the detector called X), with the confusion matrix.

Recall is measured over the worksheet's pair pool (all surfaced pairs + the
near-miss negatives); pairs excluded from the pool are assumed true negatives
-- state this boundary when reporting. Rows with empty human_label are
skipped (and counted).

Usage: python score_verification.py [--worksheet PATH] [--exclude-synthetic]
"""
import argparse
import json
from collections import Counter

DEFAULT_WS = "data/verification/verification_reviewed.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worksheet", default=DEFAULT_WS)
    ap.add_argument("--exclude-synthetic", action="store_true",
                    help="drop rows involving the injected synthetic pair")
    args = ap.parse_args()

    rows = json.load(open(args.worksheet))
    if args.exclude_synthetic:
        rows = [r for r in rows if not r.get("synthetic")]
    unlabelled = [r for r in rows if not r.get("human_label")]
    rows = [r for r in rows if r.get("human_label")]

    agree = sum(1 for r in rows if r["human_label"] == r["detector_verdict"])
    print(f"rows scored: {len(rows)}  (skipped unlabelled: {len(unlabelled)})")
    print(f"overall agreement: {agree}/{len(rows)} = {agree/len(rows):.1%}\n")

    for check in ("contradiction", "redundancy", "cross_regulation"):
        sub = [r for r in rows if r["check"] == check]
        if not sub:
            continue
        n_agree = sum(1 for r in sub if r["human_label"] == r["detector_verdict"])
        print(f"== {check}  ({len(sub)} pairs, agreement {n_agree}/{len(sub)})")
        classes = sorted({r["detector_verdict"] for r in sub}
                         | {r["human_label"] for r in sub})
        for cls in classes:
            if cls == "none":
                continue
            det = [r for r in sub if r["detector_verdict"] == cls]
            hum = [r for r in sub if r["human_label"] == cls]
            tp = sum(1 for r in det if r["human_label"] == cls)
            p = tp / len(det) if det else float("nan")
            rc = tp / len(hum) if hum else float("nan")
            print(f"   {cls:25s} P = {tp}/{len(det) or '-'} = {p:.2f}   "
                  f"R = {tp}/{len(hum) or '-'} = {rc:.2f}")
        conf = Counter((r["detector_verdict"], r["human_label"]) for r in sub
                       if r["detector_verdict"] != r["human_label"])
        for (d, h), n in sorted(conf.items()):
            print(f"   confusion: detector={d} -> human={h}  x{n}")
        print()

    print("note: recall is relative to the worksheet pool (surfaced + "
          "near-miss pairs);\npairs with zero evidence signals were excluded "
          "as assumed true negatives.")


if __name__ == "__main__":
    main()
