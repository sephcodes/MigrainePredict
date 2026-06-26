#!/usr/bin/env python3
"""
score_extraction.py  --  P/R/F1/F2 scoring layer over the gold-comparison harness.

This is the *evaluation* layer, distinct from compare_to_gold.py (the CI
regression harness). It reuses compare_to_gold.py's alignment and field-tier
grading verbatim, then projects the HARD/soft outcome onto a confusion matrix
and reports Precision / Recall / F1 / F2 — overall and per statement-class —
aggregated as a mean over N runs with [min, max].

The HARD/soft -> confusion-matrix mapping (see evaluation_methodology.md for the
full rationale) is the methodological choice this script encodes:

  Record buckets after alignment:
    M_clean = a gold record matched by a run record with ZERO hard failures.
    M_hard  = a gold record matched by a run record with >=1 hard failure.
    MISS    = a gold record with no matching run record.
    EXTRA   = a run record with no matching gold record.

  Scoring scheme = LENIENT-RECALL (the adjudicated convention):
    TP = M_clean
    FP = M_hard + EXTRA          (extracted, but wrong / unwarranted)
    FN = MISS                    (a gold proposition never surfaced)
    P  = TP / (TP + FP) = M_clean / (M_clean + M_hard + EXTRA)
    R  = TP / (TP + FN) = M_clean / (M_clean + MISS)

  A HARD-failed match (M_hard) is charged to PRECISION only: the extractor did
  correctly *detect* a proposition at that (paragraph, discriminator) key, so we
  decline to also count it as a recall miss. The stricter alternative would add
  M_hard to FN as well (penalising both P and R); we report M_hard explicitly so
  that choice stays inspectable rather than silent.

  SOFT flags do NOT enter the confusion matrix at all: a soft mismatch
  (predicate wording, subject voice, condition phrasing, references, ...) does
  not break the proposition, so the record counts as M_clean for P/R/F1/F2. Soft
  flags are reported separately as a quality layer (mean soft flags / record).

Usage
-----
  # Score one or more sets; each set = name:gold.jsonl:runs (a dir of
  # runN.extracted.jsonl, or a single .jsonl run file).
  python score_extraction.py \
      --set dev:data/gold_set.jsonl:data/dev_5run_prednorm \
      --set holdout:data/holdout_gold_set.jsonl:data/holdout_5run_newgold

  # Self-test: grading a gold file against itself must give P=R=F1=F2=1.0,
  # 0 soft, and zero records in every bucket but M_clean.
  python score_extraction.py --selftest data/gold_set.jsonl
"""
import argparse
import glob
import os
import sys
from collections import defaultdict

import compare_to_gold as ctg

CLASSES = ["DEONTIC", "DEFINITIONAL", "APPLICABILITY", "NOT_APPLICABLE"]
# The three headline categories the report breaks out (NA is reported too, for
# completeness, but is not one of the substantive extraction targets).
HEADLINE = ["DEONTIC", "DEFINITIONAL", "APPLICABILITY"]


# ----------------------------------------------------------------------------
# Core: align one (gold, run) pair the SAME way compare_to_gold.main() does,
# then bucket every record. Returns per-class counts + soft tally.
# ----------------------------------------------------------------------------
def bucket_run(gold_recs, run_recs):
    """Replicate compare_to_gold's two-pass alignment (sid_map then grade) and
    classify each record into M_clean / M_hard / MISS / EXTRA, keyed by class.

    Returns dict: class -> {"clean","hard","miss","extra"} ints,
    plus ("soft_total", "graded_pairs") for the soft quality layer."""
    from collections import OrderedDict

    gold_groups = OrderedDict()
    for r in gold_recs:
        gold_groups.setdefault(ctg.key(r), []).append(r)
    run_groups = defaultdict(list)
    for r in run_recs:
        run_groups[ctg.key(r)].append(r)

    ordered_keys = list(gold_groups.keys()) + [
        k for k in run_groups if k not in gold_groups
    ]

    # Pass 1: match + build the global run->gold statement_id map.
    group_results, sid_map = {}, {}
    for k in ordered_keys:
        pairs, missing, extra = ctg.match_group(
            gold_groups.get(k, []), run_groups.get(k, [])
        )
        group_results[k] = (pairs, missing, extra)
        for g, r in pairs:
            if r.get("statement_id") and g.get("statement_id"):
                sid_map[r["statement_id"]] = g["statement_id"]

    # Pass 2: grade and bucket.
    counts = {c: {"clean": 0, "hard": 0, "miss": 0, "extra": 0} for c in CLASSES}
    soft_total = 0
    graded_pairs = 0
    for k in ordered_keys:
        pairs, missing, extra = group_results[k]
        for g, r in pairs:
            graded_pairs += 1
            cls = g.get("statement_class") or "NOT_APPLICABLE"
            hard, soft = ctg.compare_pair(g, r, sid_map)
            soft_total += len(soft)
            counts[cls]["hard" if hard else "clean"] += 1
        for g in missing:
            cls = g.get("statement_class") or "NOT_APPLICABLE"
            counts[cls]["miss"] += 1
        for r in extra:
            cls = r.get("statement_class") or "NOT_APPLICABLE"
            counts[cls]["extra"] += 1
    return counts, soft_total, graded_pairs


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------
def fbeta(p, r, beta):
    b2 = beta * beta
    denom = b2 * p + r
    return (1 + b2) * p * r / denom if denom else 0.0


def metrics_from_counts(c):
    """c = {"clean","hard","miss","extra"}. Lenient-recall scheme."""
    tp = c["clean"]
    fp = c["hard"] + c["extra"]
    fn = c["miss"]
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "P": p, "R": r, "F1": fbeta(p, r, 1), "F2": fbeta(p, r, 2),
        "TP": tp, "FP": fp, "FN": fn,
        "clean": c["clean"], "hard": c["hard"], "miss": c["miss"], "extra": c["extra"],
    }


def overall_counts(counts):
    tot = {"clean": 0, "hard": 0, "miss": 0, "extra": 0}
    for c in CLASSES:
        for k in tot:
            tot[k] += counts[c][k]
    return tot


def pool(count_dicts):
    """Sum a list of per-class count dicts (used to build COMBINED-50)."""
    out = {c: {"clean": 0, "hard": 0, "miss": 0, "extra": 0} for c in CLASSES}
    for cd in count_dicts:
        for c in CLASSES:
            for k in out[c]:
                out[c][k] += cd[c][k]
    return out


# ----------------------------------------------------------------------------
# Aggregation across runs
# ----------------------------------------------------------------------------
def aggregate(per_run_metric_dicts, field):
    """Given a list of metric dicts (one per run), return (mean, min, max) of
    `field` across runs."""
    vals = [m[field] for m in per_run_metric_dicts]
    return (sum(vals) / len(vals), min(vals), max(vals)) if vals else (0.0, 0.0, 0.0)


def discover_runs(spec):
    """spec is a dir of runN.extracted.jsonl, or a single .jsonl file."""
    if os.path.isdir(spec):
        files = sorted(glob.glob(os.path.join(spec, "run*.extracted.jsonl")))
        if not files:
            files = sorted(glob.glob(os.path.join(spec, "*.jsonl")))
        return files
    return [spec]


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
# Column widths: a metric cell is "0.846 [0.821, 0.852]" = 20 chars.
_LABEL_W = 14
_CELL_W = 20


def _fmt(triple):
    mean, lo, hi = triple
    return f"{mean:5.3f} [{lo:5.3f}, {hi:5.3f}]"


def _row(label, cells):
    """Render one table row: label column + ' | '-separated metric cells."""
    parts = [f"{label:<{_LABEL_W}}"] + [f"{c:^{_CELL_W}}" for c in cells]
    return " | ".join(parts)


def _rule(char="-"):
    n = _LABEL_W + 4 * (_CELL_W + 3)
    return char * n


def report_block(title, per_run_counts, per_run_soft, per_run_pairs):
    """per_run_counts: list[ per-class count dict ] (one per run)."""
    n = len(per_run_counts)
    print("=" * 78)
    print(f"{title}   (mean over {n} run{'s' if n != 1 else ''}, [min, max])")
    print("=" * 78)

    rows = HEADLINE + ["NOT_APPLICABLE", "__OVERALL__"]
    print(_row("category", ["P", "R", "F1", "F2"]))
    print(_rule("-"))
    for row in rows:
        if row == "__OVERALL__":
            class_counts = [overall_counts(c) for c in per_run_counts]
            label = "OVERALL"
        else:
            class_counts = [c[row] for c in per_run_counts]
            label = row.title() if row != "NOT_APPLICABLE" else "Not_Applicable"
        # Suppress a category that has no records at all (no gold, no run) so an
        # empty 0/0 doesn't read as a real 0.000 score.
        if all(sum(cc.values()) == 0 for cc in class_counts):
            print(_row(label, ["— (no records)", "", "", ""]))
            continue
        per_run_m = [metrics_from_counts(cc) for cc in class_counts]
        print(_row(label, [_fmt(aggregate(per_run_m, m)) for m in ("P", "R", "F1", "F2")]))

    # Mean raw counts (auditable) + soft quality layer, overall.
    ov = [overall_counts(c) for c in per_run_counts]
    def cmean(k):
        return sum(o[k] for o in ov) / len(ov)
    print(_rule("-"))
    print(f"mean counts  M_clean(TP)={cmean('clean'):.1f}  "
          f"M_hard={cmean('hard'):.1f}  MISS(FN)={cmean('miss'):.1f}  "
          f"EXTRA={cmean('extra'):.1f}")
    soft_mean = sum(per_run_soft) / len(per_run_soft)
    pairs_mean = sum(per_run_pairs) / len(per_run_pairs)
    per_rec = soft_mean / pairs_mean if pairs_mean else 0.0
    print(f"soft quality layer  mean soft flags={soft_mean:.1f}  "
          f"over {pairs_mean:.1f} matched records  ({per_rec:.2f}/record)  "
          f"[soft = correct for P/R/F1/F2]")
    print()


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="NAME:GOLD:RUNS",
                    help="A scored set. RUNS = dir of runN.extracted.jsonl or a .jsonl file.")
    ap.add_argument("--selftest", metavar="GOLD",
                    help="Grade GOLD against itself; must yield P=R=F1=F2=1.0, 0 soft.")
    ap.add_argument("--no-combined", action="store_true",
                    help="Skip the pooled COMBINED block when >1 set is given.")
    args = ap.parse_args()

    if args.selftest:
        gold = ctg.load(args.selftest)
        gold_scored = [r for r in gold if not r.get("screen_dependent")]
        counts, soft, pairs = bucket_run(gold_scored, gold_scored)
        report_block(f"SELF-TEST  {os.path.basename(args.selftest)}",
                     [counts], [soft], [pairs])
        ov = metrics_from_counts(overall_counts(counts))
        ok = (abs(ov["P"] - 1.0) < 1e-9 and abs(ov["R"] - 1.0) < 1e-9
              and soft == 0 and ov["hard"] == 0 and ov["miss"] == 0 and ov["extra"] == 0)
        print("SELF-TEST", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)

    if not args.set:
        ap.error("provide at least one --set NAME:GOLD:RUNS (or --selftest)")

    # Parse sets, align run files by index for the COMBINED pooling.
    parsed = []
    for s in args.set:
        name, gold_path, runs_spec = s.split(":", 2)
        gold = [r for r in ctg.load(gold_path) if not r.get("screen_dependent")]
        run_files = discover_runs(runs_spec)
        if not run_files:
            ap.error(f"no run files found for set {name} at {runs_spec}")
        per_run = []  # list of (counts, soft, pairs)
        for rf in run_files:
            per_run.append(bucket_run(gold, ctg.load(rf)))
        parsed.append((name, len(gold), per_run))

    for name, ngold, per_run in parsed:
        report_block(f"SET: {name}  ({ngold} gold records)",
                     [c for c, _, _ in per_run],
                     [s for _, s, _ in per_run],
                     [p for _, _, p in per_run])

    # COMBINED: pool counts across sets, run-index by run-index.
    if len(parsed) > 1 and not args.no_combined:
        nruns = min(len(pr) for _, _, pr in parsed)
        if any(len(pr) != nruns for _, _, pr in parsed):
            print(f"[note] sets have differing run counts; pooling first {nruns} "
                  f"runs by index for COMBINED.")
        combined_counts, combined_soft, combined_pairs = [], [], []
        for i in range(nruns):
            combined_counts.append(pool([pr[i][0] for _, _, pr in parsed]))
            combined_soft.append(sum(pr[i][1] for _, _, pr in parsed))
            combined_pairs.append(sum(pr[i][2] for _, _, pr in parsed))
        total_gold = sum(ng for _, ng, _ in parsed)
        report_block(f"COMBINED  ({total_gold} gold records)",
                     combined_counts, combined_soft, combined_pairs)


if __name__ == "__main__":
    main()
