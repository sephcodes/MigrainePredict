#!/usr/bin/env python3
"""
cross_run_agreement.py  --  cross-run extraction stability (LLM stochasticity).

Aligns the N runs of a set into extraction "slots" and measures how consistently
the runs agree, per slot and per field. Disagreement localises where the
extractor is unstable (e.g. the Annex III applies_to oscillating between
"Biometrics" and "AI systems" surfaces as a HARD field disagreement).

Reuses compare_to_gold verbatim so the comparison is the project's usual DEEP
comparison, not literal string equality:
  - alignment by (paragraph_iri, discriminator), with content best-match
    (_score_pair) for the rare keys holding >1 statement per run;
  - field values canonicalised with norm_text / predicate-norm / depth-tolerant
    refs, and split into the same HARD (objective) vs soft (interpretive) tiers.

A KEY field (modality / scope_type / polarity / term) is constant within a slot
by construction, so its instability shows up as PRESENCE instability (the slot
appears in only k/N runs) rather than a field disagreement — still localised to
that paragraph. Non-key fields (applies_to.value, definition.value, predicate,
object, condition, ...) are compared across the runs present in the slot.

No gold, no LLM. Model-agnostic: cross-model agreement is just another set of
runs dropped in as --set.

Usage:
  python cross_run_agreement.py \
      --set dev:data/dev_5run_prednorm \
      --set holdout:data/holdout_5run_newgold

Each --set is NAME:RUNS (RUNS = a dir of runN.extracted.jsonl or a single .jsonl).
"""
import argparse
import glob
import json
import os
from collections import defaultdict

import compare_to_gold as ctg


def discover_runs(spec):
    if os.path.isdir(spec):
        files = sorted(glob.glob(os.path.join(spec, "run*.extracted.jsonl")))
        return files or sorted(glob.glob(os.path.join(spec, "*.jsonl")))
    return [spec]


def cluster(run_to_recs):
    """Greedy content clustering within one (iri, discriminator) key, one record
    per run per slot. For the common 1-record-per-run key this yields a single
    slot holding the runs' records; for a multi-statement key it aligns siblings
    by content (_score_pair)."""
    slots = []  # list of {run_idx: record}
    for ri in sorted(run_to_recs):
        for rec in run_to_recs[ri]:
            cands = [s for s in slots if ri not in s]
            if cands:
                best = max(cands, key=lambda s: sum(ctg._score_pair(rec, m)
                                                     for m in s.values()) / len(s))
                best[ri] = rec
            else:
                slots.append({ri: rec})
    return slots


def field_canon(field, stmt, modality):
    """Canonical, hashable value for a field, using the harness's deep
    normalisation (mirrors compare_to_gold.compare_pair)."""
    if field == "predicate":
        return tuple(ctg.ev_list_summary_predicate(stmt.get("predicate"), modality) or [])
    if field in ("subject", "object"):
        return tuple(ctg.ev_list_summary(stmt.get(field)) or [])
    if field in ("condition", "beneficiary"):
        v = stmt.get(field)
        if isinstance(v, dict):
            return ctg.norm_text(v.get("value"))
        return ctg.norm_text(v) if isinstance(v, str) else v
    return ctg.norm_value(field, ctg.get(field, stmt))


def graded_fields(cls):
    return ctg.OBJECTIVE_STMT.get(cls, []) + ctg.INTERPRETIVE_STMT.get(cls, [])


def field_tier(cls, field):
    if field in ctg.OBJECTIVE_STMT.get(cls, []):
        return "soft" if field.endswith("references") else "HARD"
    return "soft"


def analyse_slot(cls, slot, nruns):
    """Return (present_count, [(tier, field, {canon: [runs]})])."""
    present = sorted(slot)
    disagreements = []
    for field in graded_fields(cls):
        vals = defaultdict(list)
        for ri in present:
            stmt = slot[ri].get("statement") or {}
            vals[field_canon(field, stmt, stmt.get("modality"))].append(ri)
        if len(vals) > 1:
            disagreements.append((field_tier(cls, field), field, dict(vals)))
    return len(present), disagreements


def _show(v):
    if isinstance(v, frozenset):
        return "{" + ", ".join(sorted(str(x) for x in v)) + "}" if v else "∅"
    if isinstance(v, tuple):
        return "[" + " | ".join(str(x) for x in v) + "]" if v else "∅"
    if v is None or v == "":
        return "∅"
    s = str(v)
    return s if len(s) <= 70 else s[:67] + "..."


def analyse_set(name, run_files):
    runs = [ctg.load(f) for f in run_files]
    nruns = len(runs)

    per_key = defaultdict(lambda: defaultdict(list))  # key -> run_idx -> [recs]
    for i, run in enumerate(runs):
        for r in run:
            per_key[ctg.key(r)][i].append(r)

    slots = []  # (key, cls, present_count, disagreements)
    for k, r2r in per_key.items():
        for slot in cluster(r2r):
            cls = next(iter(slot.values())).get("statement_class")
            present, dis = analyse_slot(cls, slot, nruns)
            slots.append((k, cls, present, dis))

    n = len(slots)
    present_stable = sum(1 for _, _, p, _ in slots if p == nruns)
    hard_stable = sum(1 for _, _, _, d in slots if not any(t == "HARD" for t, *_ in d))
    fully_stable = sum(1 for _, _, p, d in slots if p == nruns and not d)
    pct = lambda a: f"{100 * a / n:.1f}%" if n else "—"

    field_dis = defaultdict(int)
    for _, cls, _, d in slots:
        for tier, field, _ in d:
            field_dis[(tier, field)] += 1

    print("=" * 76)
    print(f"SET: {name}   ({nruns} runs, {n} aligned slots)")
    print("=" * 76)
    print(f"  present in all {nruns} runs:                {present_stable}/{n} = {pct(present_stable)}")
    print(f"  HARD-agreement (objective fields agree):  {hard_stable}/{n} = {pct(hard_stable)}")
    print(f"  fully stable (present + all fields agree): {fully_stable}/{n} = {pct(fully_stable)}")
    if field_dis:
        print("  field disagreement (slots affected):")
        for (tier, field), c in sorted(field_dis.items(), key=lambda x: (x[0][0] != "HARD", -x[1])):
            print(f"    [{tier}] {field}: {c}")

    unstable = [(k, cls, p, d) for k, cls, p, d in slots if p != nruns or d]
    print("-" * 76)
    if not unstable:
        print(f"  unstable slots: NONE — every slot is present in all {nruns} runs "
              f"and all fields agree.")
    else:
        print(f"  unstable slots ({len(unstable)}), localised:")
        for k, cls, p, d in sorted(unstable, key=lambda x: (x[2], x[0][0])):
            iri, disc = k
            tag = "" if p == nruns else f"  PRESENCE {p}/{nruns}"
            print(f"    [{cls}] {iri}  {disc[1:]}{tag}")
            for tier, field, vals in sorted(d, key=lambda x: x[0] != "HARD"):
                variants = "   ".join(f"{_show(v)}×{len(rs)}" for v, rs in vals.items())
                print(f"        {tier} {field}: {variants}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], required=True,
                    metavar="NAME:RUNS",
                    help="RUNS = dir of runN.extracted.jsonl or a single .jsonl file.")
    args = ap.parse_args()
    for s in args.set:
        name, runs_spec = s.split(":", 1)
        run_files = discover_runs(runs_spec)
        if len(run_files) < 2:
            ap.error(f"need >=2 runs for set {name} at {runs_spec}")
        analyse_set(name, run_files)


if __name__ == "__main__":
    main()
