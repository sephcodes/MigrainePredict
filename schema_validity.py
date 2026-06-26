#!/usr/bin/env python3
"""
schema_validity.py  --  Pydantic schema-validity rate of stage-2 extractions.

Two measures over saved runs (no gold, no LLM):

1. First-attempt pass rate. The pipeline validates with
   `llm.with_structured_output(Model)` and does NOT retry on a Pydantic failure
   (extract_min._retry_invoke retries only on transient network errors), so a
   schema/parse failure surfaces as a saved record with `extractor_error` set
   (or `statement: null`). First-attempt pass = the call produced a valid
   statement = it has no extractor_error and a non-null statement.
       rate = valid / (valid + failed)
   Failures are split into schema vs transient (503/timeout) using
   extract_min._TRANSIENT_ERROR_MARKERS, so network blips don't count against
   schema validity.

2. Re-validation against the current schema. Each surviving statement dict is
   re-instantiated through its actual Pydantic model (DeonticStatement /
   DefinitionalStatement / ApplicabilityStatement). This confirms the saved
   output still conforms to the current schema and catches drift since extraction.

Scope = stage-2 TYPED extractions only (DEONTIC / DEFINITIONAL / APPLICABILITY);
NOT_APPLICABLE stubs and stage-1 classification are not stage-2 statement objects
and are excluded.

Usage:
  python schema_validity.py \
      --set dev:data/dev_5run_prednorm \
      --set holdout:data/holdout_5run_newgold

Each --set is NAME:RUNS (RUNS = a dir of runN.extracted.jsonl or a single .jsonl).
"""
import argparse
import glob
import json
import os
from collections import Counter

from pydantic import ValidationError

from extract_min import (
    DeonticStatement, DefinitionalStatement, ApplicabilityStatement,
    _TRANSIENT_ERROR_MARKERS,
)

MODELS = {
    "DEONTIC": DeonticStatement,
    "DEFINITIONAL": DefinitionalStatement,
    "APPLICABILITY": ApplicabilityStatement,
}


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def discover_runs(spec):
    if os.path.isdir(spec):
        files = sorted(glob.glob(os.path.join(spec, "run*.extracted.jsonl")))
        return files or sorted(glob.glob(os.path.join(spec, "*.jsonl")))
    return [spec]


def _is_transient(msg):
    return any(m in (msg or "") for m in _TRANSIENT_ERROR_MARKERS)


def check_set(name, run_files):
    total = Counter()          # class -> typed extraction attempts
    valid = Counter()          # class -> first-attempt valid
    schema_fail, transient_fail = [], []   # (class, iri, msg)
    revalid_ok = Counter()
    revalid_fail = []          # (class, iri, error)

    for rf in run_files:
        for r in load_jsonl(rf):
            cls = r.get("statement_class")
            if cls not in MODELS:
                continue
            total[cls] += 1
            err = r.get("extractor_error")
            stmt = r.get("statement")
            if err or stmt is None:
                (transient_fail if _is_transient(err) else schema_fail).append(
                    (cls, r.get("paragraph_iri"), err))
                continue
            valid[cls] += 1
            try:
                MODELS[cls].model_validate(stmt)
                revalid_ok[cls] += 1
            except ValidationError as e:
                revalid_fail.append((cls, r.get("paragraph_iri"),
                                     str(e).splitlines()[0]))

    n = len(run_files)
    tot = sum(total.values())
    val = sum(valid.values())
    rev = sum(revalid_ok.values())
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "—"

    print(f"=== SET: {name}   ({n} run{'s' if n != 1 else ''}) ===")
    by_class = ", ".join(f"{c} {total[c]}" for c in MODELS if total[c])
    print(f"  stage-2 typed extractions: {tot}   ({by_class})")
    print(f"  first-attempt Pydantic pass: {val}/{tot} = {pct(val, tot)}"
          f"   (failures: {len(schema_fail)} schema, {len(transient_fail)} transient)")
    print(f"  re-validation vs current schema: {rev}/{val} = {pct(rev, val)}")
    for cls in MODELS:
        if total[cls]:
            print(f"      {cls:<13} first-attempt {valid[cls]}/{total[cls]}"
                  f"   re-validate {revalid_ok[cls]}/{valid[cls]}")
    for label, items in [("SCHEMA FAILURE", schema_fail),
                         ("transient failure", transient_fail),
                         ("RE-VALIDATION FAILURE", revalid_fail)]:
        for cls, iri, msg in items:
            print(f"    [{label}] {cls} {iri}: {msg}")
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
        if not run_files:
            ap.error(f"no run files found for set {name} at {runs_spec}")
        check_set(name, run_files)


if __name__ == "__main__":
    main()
