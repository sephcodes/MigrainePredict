#!/usr/bin/env python3
"""
span_grounding.py  --  flag STATED elements whose content is not in the source.

For every element marked method="STATED", check that its content words actually
occur in the source the model was given (the paragraph plus its parent lead-in
chain). An element is FLAGGED when it contains a content word that is absent
from the source — i.e. the model asserted "STATED" but used material that is not
there. No gold reference is used.

Comparison reuses the project's existing word-level machinery rather than
literal string matching: predicate_norm's WordNet lemmatiser (so objected/
objects and demonstrate/demonstrates collapse symmetrically) and a stop-list of
nltk's English stopwords plus the pipeline's _PRED_STOP modals. It is therefore
tolerant of the things that are NOT hallucination: omission/recomposition
(dropped or reordered clauses), inflection, and inserted function words/modals
(that, it, shall, of, when ...). Only an invented content-word lemma is flagged.

`predicate` is excluded: the pipeline deliberately lemmatises predicate
verb-forms (predicate_norm.py) and the source often nominalises them
("rectification" vs "rectify"), so a predicate mismatch is a normalisation
artifact, not a model hallucination.

Usage:
  python span_grounding.py \
      --set dev:data/gdpr.smoke_curated.postscreened.jsonl,data/aiact.smoke_curated.postscreened.jsonl:data/dev_5run_prednorm \
      --set holdout:data/holdout.postscreened.jsonl:data/holdout_5run_newgold

Each --set is NAME:INPUTS:RUNS (INPUTS = comma-separated postscreened file(s);
RUNS = a dir of runN.extracted.jsonl or a single .jsonl run file).
"""
import argparse
import glob
import json
import os
import re
from collections import Counter

import nltk

import extract_min as em
from predicate_norm import _LEM  # WordNet lemmatiser (nltk download already guarded)

CONTENT_FIELDS = {"subject", "object", "condition", "beneficiary",
                  "applies_to", "definition"}


def _stoplist():
    """nltk English stopwords (function words) ∪ the pipeline's modal list."""
    try:
        nltk.data.find("corpora/stopwords")
    except LookupError:
        try:
            nltk.download("stopwords", quiet=True)
        except Exception:
            pass
    try:
        from nltk.corpus import stopwords
        return set(stopwords.words("english")) | set(em._PRED_STOP)
    except Exception:
        return set(em._PRED_STOP)


_STOP = _stoplist()


def _lemma(t):
    """Symmetric lemma: verb form then noun form, so plural/tense variants
    (demonstrates→demonstrate, provisions→provision) collapse both sides."""
    return _LEM.lemmatize(_LEM.lemmatize(t, pos="v"), pos="n")


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def content_lemmas(text):
    """Content-word lemma set (stop-words dropped) of a string."""
    return {_lemma(t) for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOP}


def source_lemmas(rec):
    """Content-word lemma set of the source: paragraph text + parent chain."""
    parts = [rec.get("text") or ""]
    parts += [p.get("text") or "" for p in (rec.get("parent") or [])]
    return content_lemmas(" ".join(parts))


def walk_stated(obj, field=None):
    """Yield (field, value) for each {value, method:STATED} node."""
    if isinstance(obj, dict):
        if "value" in obj and "method" in obj:
            if obj.get("method") == "STATED":
                yield field, obj.get("value")
            return
        for k, v in obj.items():
            yield from walk_stated(v, field if field is not None else k)
    elif isinstance(obj, list):
        for x in obj:
            yield from walk_stated(x, field)


def discover_runs(spec):
    if os.path.isdir(spec):
        files = sorted(glob.glob(os.path.join(spec, "run*.extracted.jsonl")))
        return files or sorted(glob.glob(os.path.join(spec, "*.jsonl")))
    return [spec]


def check_set(name, input_paths, run_files):
    idx = {}
    for p in input_paths:
        for rec in load_jsonl(p):
            idx[rec.get("iri")] = rec

    total = 0
    flags = Counter()      # (field, iri, value) -> runs flagged
    missing_of = {}        # (field, iri, value) -> out-of-source words
    for rf in run_files:
        for r in load_jsonl(rf):
            src = source_lemmas(idx.get(r.get("paragraph_iri"), {}))
            for field, value in walk_stated(r.get("statement") or {}):
                if field not in CONTENT_FIELDS:
                    continue
                total += 1
                missing = [t for t in re.findall(r"[a-z0-9]+", (value or "").lower())
                           if t not in _STOP and _lemma(t) not in src]
                if missing:
                    k = (field, r.get("paragraph_iri"), value)
                    flags[k] += 1
                    missing_of[k] = missing

    n = len(run_files)
    print(f"=== {name}: {len(flags)} flagged / {total} STATED content elements "
          f"over {n} run(s) ===")
    if not flags:
        print("  none — every STATED content value is grounded in its source.\n")
        return
    for (field, iri, value), runs in flags.most_common():
        v = value if len(value) <= 90 else value[:87] + "..."
        print(f"  {runs}/{n} [{field}] {iri}  not-in-source={missing_of[(field, iri, value)]}")
        print(f"        {v!r}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], required=True,
                    metavar="NAME:INPUTS:RUNS",
                    help="INPUTS = comma-separated postscreened file(s); "
                         "RUNS = dir of runN.extracted.jsonl or a .jsonl file.")
    args = ap.parse_args()
    for s in args.set:
        name, inputs, runs_spec = s.split(":", 2)
        run_files = discover_runs(runs_spec)
        if not run_files:
            ap.error(f"no run files found for set {name} at {runs_spec}")
        check_set(name, inputs.split(","), run_files)


if __name__ == "__main__":
    main()
