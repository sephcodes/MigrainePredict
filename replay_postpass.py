#!/usr/bin/env python3
"""
replay_postpass.py  --  re-apply the current deterministic post-pass suite to a
saved extractor run, with no LLM calls.

The stage-1/stage-2 LLM output in a saved run is frozen; only the deterministic
post-passes have changed since. This replays the exact post-pass sequence from
extract_min._process_paragraph onto a saved run, bringing it up to date at zero
API cost. (It does not reproduce the stage-1->2 enumeration gate, which can
re-route a candidate to a different LLM extractor — only valid when the run
already postdates the relevant gate change.)

Usage:
  # Dry run: write <run>.replayed.jsonl and print the diff
  python replay_postpass.py \
      --input data/holdout.postscreened.jsonl \
      --run   data/holdout_5run_newgold/run1.extracted.jsonl

  # Overwrite in place (after inspecting a dry-run diff)
  python replay_postpass.py --input ... --run ... --in-place
"""
import argparse
import json
from collections import OrderedDict, defaultdict

import extract_min as em


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def build_input_index(input_paths):
    """iri -> input paragraph rec (supplies iri/text/unit_type/parent)."""
    idx = {}
    for p in input_paths:
        for rec in load_jsonl(p):
            idx[rec.get("iri")] = rec
    return idx


def run_postpasses(rec, results):
    """The exact post-pass sequence from extract_min._process_paragraph.
    Deterministic; mutates `results` in place (may drop entries)."""
    iri = rec["iri"]
    em._normalise_predicates(rec, results)
    em._fix_annex_area_applies_to(rec, results)
    em._canonicalize_subjects(rec, results)
    em._guard_recital_applicability(rec, results)
    em._merge_exception_splits(rec, results)
    em._assign_statement_ids(iri, results)
    em._link_intra_paragraph_parents(results)
    em._flag_smeared_references(rec, results)
    em._flag_truncated_spans(rec, results)
    em._flag_deontic_operator_predicate(rec, results)
    em._flag_redundant_negation(rec, results)
    return results


def replay_run(input_paths, run_path, out_path):
    idx = build_input_index(input_paths)
    run = load_jsonl(run_path)

    # Group run records by paragraph, then replay the post-passes per paragraph.
    groups = OrderedDict()
    for r in run:
        groups.setdefault(r.get("paragraph_iri"), []).append(r)

    new_records, missing_input = [], []
    for piri, results in groups.items():
        rec = idx.get(piri)
        if rec is None:
            missing_input.append(piri)      # no input rec; pass through untouched
            new_records.extend(results)
            continue
        copy = json.loads(json.dumps(results, ensure_ascii=False))  # fresh dicts
        new_records.extend(run_postpasses(rec, copy))

    # Diff against the original, per paragraph.
    after = defaultdict(list)
    for r in new_records:
        after[r.get("paragraph_iri")].append(r)
    norm = lambda o: json.dumps(o, ensure_ascii=False, sort_keys=True)

    diffs = []
    for piri, before in groups.items():
        a = after.get(piri, [])
        if len(before) != len(a):
            diffs.append(f"  [count]   {piri}: {len(before)} -> {len(a)} records")
        elif sorted(norm(x) for x in before) != sorted(norm(x) for x in a):
            diffs.append(f"  [content] {piri}: {len(before)} records, fields changed")

    print(f"replay: {run_path}")
    print(f"  {len(run)} records -> {len(new_records)} across {len(groups)} paragraph(s); "
          f"{len(diffs)} paragraph(s) changed")
    if missing_input:
        print(f"  WARNING: no input rec for {len(missing_input)} paragraph(s), "
              f"passed through untouched: {missing_input}")
    for d in diffs:
        print(d)

    with open(out_path, "w", encoding="utf-8") as f:
        for r in new_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  wrote {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", nargs="+", required=True,
                    help="postscreened input file(s) supplying paragraph recs")
    ap.add_argument("--run", required=True, help="saved run .extracted.jsonl to replay")
    ap.add_argument("--out", help="output path (default: <run>.replayed.jsonl)")
    ap.add_argument("--in-place", action="store_true",
                    help="overwrite the run file (inspect a dry-run diff first)")
    args = ap.parse_args()

    out = args.run if args.in_place else (args.out or args.run.replace(".jsonl", ".replayed.jsonl"))
    replay_run(args.input, args.run, out)


if __name__ == "__main__":
    main()
