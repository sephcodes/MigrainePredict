#!/usr/bin/env python3
"""
cycle_consistency.py  --  serialize -> re-extract -> measure field-level drift.

Tests whether the DEONTIC structured representation is lossless: render a record
back to a sentence with a DUMB, DETERMINISTIC template, feed that sentence back
through the stage-2 deontic extractor, and compare the re-extraction to the
original, field by field.

Why a dumb template (design choice, stated explicitly): an LLM-based serializer
would introduce a second model whose errors contaminate the metric — we could
not tell a low score apart from a serializer failure. The template here can only
emit what is in the record, so any drift on re-extraction is genuinely the
representation's fault, not the serializer's. It is therefore DEONTIC-only and
fixed:

    "{subject} {modality-verb} {predicate} {object}[, [where ]{condition}]."
    OBLIGATION->shall  PROHIBITION->shall not  PERMISSION->may  DISPENSATION->need not

The injected "where" is suppressed (condition comma-appended verbatim) when the
condition already opens with a subordinator/preposition/participle, via a closed
lead-word list (_NO_WHERE_LEAD) — so "where where ...", "where in ...",
"where When ..." don't occur. Still dumb and deterministic: the condition text
is emitted verbatim; only the connective is chosen by a fixed word list.

Scope of comparison: only the fields the template emits round-trip, so drift is
scored on modality (HARD) + subject/predicate/object/condition (soft). Fields
not serialised (source_article, references, beneficiary, severity,
applies_to_healthcare) are excluded — they cannot survive by construction.

Re-extraction path: stage-2 DEONTIC extractor directly (stage-1 bypassed) so the
classifier's stochasticity does not contaminate the representation signal.

Baseline correction: re-extraction is itself stochastic, so cycle drift is only
interesting where it EXCEEDS the field's ordinary run-to-run wobble. We compute
that baseline from the same N runs (deontic slots, same value canon as the drift
measure) and report drift next to baseline, flagging fields where drift>baseline.

Values are compared with the project's deep normalisation (norm_text +
predicate_norm), value-only (method tags ignored), so determiner/verb-form noise
does not count. The serialised sentence is printed for every record so a low
score can be diagnosed as serializer-degenerate vs extractor-mangled.

LIVE: this calls Gemini (K re-extractions per original record).

Usage:
  python cycle_consistency.py --k 5 \
      --set dev:data/dev_5run_prednorm \
      --set holdout:data/holdout_5run_newgold
"""
import argparse
import glob
import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import compare_to_gold as ctg
import cross_run_agreement as cra
import extract_min as em
from predicate_norm import normalise_predicate

MODAL_VERB = {"OBLIGATION": "shall", "PROHIBITION": "shall not",
              "PERMISSION": "may", "DISPENSATION": "need not"}
SCORE_FIELDS = ["modality", "predicate", "object", "subject", "condition"]
HARD_FIELDS = {"modality"}

# Condition lead-words after which an injected "where" is ungrammatical or a
# duplicate ("where where ...", "where in ...", "where When ..."). When the
# condition opens with one of these the verbatim condition is comma-appended
# without "where"; otherwise it reads as a clause and "where {condition}" is
# used. Closed list derived from the deontic conditions across both sets;
# deterministic, no LLM, condition text emitted verbatim either way.
_NO_WHERE_LEAD = {
    "where", "when", "whenever", "while", "whilst", "if", "unless", "although",
    "though", "because", "provided", "save", "except", "in", "on", "at", "to",
    "by", "for", "with", "without", "taking", "prior", "as", "after", "before",
    "during", "upon", "pursuant", "insofar", "an",
}


def discover_runs(spec):
    if os.path.isdir(spec):
        files = sorted(glob.glob(os.path.join(spec, "run*.extracted.jsonl")))
        return files or sorted(glob.glob(os.path.join(spec, "*.jsonl")))
    return [spec]


# ---------------------------------------------------------------------------
# Serializer (deterministic, dumb)
# ---------------------------------------------------------------------------
def _join(stmt, field):
    return " and ".join(e.get("value", "").strip()
                        for e in (stmt.get(field) or []) if e.get("value"))


def _cond(stmt):
    c = stmt.get("condition")
    if isinstance(c, dict):
        return (c.get("value") or "").strip()
    return c.strip() if isinstance(c, str) else ""


def serialize(stmt):
    subj = _join(stmt, "subject")
    verb = MODAL_VERB.get(stmt.get("modality"), "shall")
    pred = _join(stmt, "predicate")
    obj = _join(stmt, "object")
    s = " ".join(p for p in (subj, verb, pred, obj) if p)
    cond = _cond(stmt)
    if cond:
        lead = (re.findall(r"[a-z]+", cond.lower()) or [""])[0]
        s += f", {cond}" if lead in _NO_WHERE_LEAD else f", where {cond}"
    s = s.strip()
    return (s[0].upper() + s[1:] + ".") if s else ""


# ---------------------------------------------------------------------------
# Value canon (value-only, deep-normalised; shared by drift + baseline)
# ---------------------------------------------------------------------------
def vcanon(field, stmt):
    if field == "modality":
        return stmt.get("modality")
    if field == "predicate":
        return frozenset(
            ctg.norm_text(normalise_predicate(e.get("value") or "", stmt.get("modality"))[0])
            for e in (stmt.get("predicate") or []) if e.get("value"))
    if field in ("subject", "object"):
        return frozenset(ctg.norm_text(e.get("value"))
                         for e in (stmt.get(field) or []) if e.get("value"))
    if field == "condition":
        return ctg.norm_text(_cond(stmt)) or None
    return None


# ---------------------------------------------------------------------------
# Baseline wobble (deontic slots, same canon)
# ---------------------------------------------------------------------------
def baseline(run_files):
    runs = [ctg.load(f) for f in run_files]
    per_key = defaultdict(lambda: defaultdict(list))
    for i, run in enumerate(runs):
        for r in run:
            if r.get("statement_class") == "DEONTIC":
                per_key[ctg.key(r)][i].append(r)
    dis = Counter()
    slots = 0
    for r2r in per_key.values():
        for slot in cra.cluster(r2r):
            if len(slot) < 2:
                continue
            slots += 1
            for f in SCORE_FIELDS:
                if len({vcanon(f, slot[ri].get("statement") or {}) for ri in slot}) > 1:
                    dis[f] += 1
    return {f: (dis[f] / slots if slots else 0.0) for f in SCORE_FIELDS}, slots


# ---------------------------------------------------------------------------
# Re-extraction (live, stage-2 deontic direct)
# ---------------------------------------------------------------------------
def reextract(chain, sentence):
    rec = {"iri": "synthetic:cycle/par_0", "text": sentence, "source": "synthetic",
           "unit_type": "article", "unit_number": "0", "parent": []}
    ctx = em.build_context_bundle(rec)
    try:
        stmt = em._retry_invoke(chain, {"context_bundle": ctx, "anchor": sentence},
                                label="cycle reextract")
    except Exception as e:
        return None, str(e)
    return stmt.model_dump(mode="json"), None


# ---------------------------------------------------------------------------
def run_set(name, run_files, K, chain):
    base, base_slots = baseline(run_files)
    originals = [r for r in ctg.load(run_files[0])
                 if r.get("statement_class") == "DEONTIC"]

    rows = []          # (iri, sentence, orig_stmt, [reextracted_stmt or None]*K)
    skipped = []
    tasks = []         # (row_idx, sentence)
    for r in originals:
        stmt = r.get("statement") or {}
        sent = serialize(stmt)
        if not _join(stmt, "predicate") or not sent:
            skipped.append(r.get("paragraph_iri"))
            continue
        rows.append([r.get("paragraph_iri"), sent, stmt, [None] * K])
        for k in range(K):
            tasks.append((len(rows) - 1, k, sent))

    # Live re-extraction (concurrent).
    def work(t):
        idx, k, sent = t
        out, err = reextract(chain, sent)
        return idx, k, out, err
    with ThreadPoolExecutor(max_workers=8) as ex:
        for idx, k, out, err in ex.map(work, tasks):
            rows[idx][3][k] = out

    # Drift accounting.
    drift = Counter()          # field -> drift events
    cmp_n = Counter()          # field -> comparisons made (recovered re-extractions)
    recovered = total = 0
    per_record = []
    for iri, sent, orig, rexs in rows:
        rec_drift = defaultdict(int)
        rec_cmp = 0
        for re in rexs:
            total += 1
            if re is None:
                continue
            recovered += 1
            rec_cmp += 1
            for f in SCORE_FIELDS:
                cmp_n[f] += 1
                if vcanon(f, orig) != vcanon(f, re):
                    drift[f] += 1
                    rec_drift[f] += 1
        per_record.append((iri, sent, orig, rec_cmp, dict(rec_drift), rexs))

    print("=" * 80)
    print(f"SET: {name}   ({len(rows)} deontic originals × K={K} re-extractions, "
          f"baseline over {base_slots} multi-run deontic slots)")
    if skipped:
        print(f"  skipped {len(skipped)} record(s) with no serialisable predicate: {skipped}")
    print(f"  re-extraction recovery: {recovered}/{total}")
    print("=" * 80)
    print(f"  {'field':<11}{'baseline':>10}{'round-trip':>12}{'excess':>9}   tier")
    for f in SCORE_FIELDS:
        d = drift[f] / cmp_n[f] if cmp_n[f] else 0.0
        b = base[f]
        exc = d - b
        flag = "  <-- drift > baseline" if exc > 1e-9 else ""
        tier = "HARD" if f in HARD_FIELDS else "soft"
        print(f"  {f:<11}{b:>10.3f}{d:>12.3f}{exc:>+9.3f}   {tier}{flag}")

    print("-" * 80)
    print("  per-record (serialised sentence + fields that drifted):")
    for iri, sent, orig, rec_cmp, rec_drift, rexs in per_record:
        tag = ""
        if rec_drift:
            tag = "  DRIFT: " + ", ".join(
                f"{f}({n}/{rec_cmp})" for f, n in rec_drift.items())
        print(f"    [{orig.get('modality')}] {iri}{tag}")
        print(f"        \"{sent}\"")
        for f, n in rec_drift.items():
            variants = Counter(_show(vcanon(f, re)) for re in rexs if re is not None)
            print(f"        {f}: orig={_show(vcanon(f, orig))}  re={dict(variants)}")
    print()


def _show(v):
    if isinstance(v, frozenset):
        return "{" + ", ".join(sorted(str(x) for x in v)) + "}" if v else "∅"
    if v is None or v == "":
        return "∅"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], required=True,
                    metavar="NAME:RUNS")
    ap.add_argument("--k", type=int, default=5, help="re-extractions per sentence")
    ap.add_argument("--backend", default="gemini", choices=["gemini", "mistral"])
    args = ap.parse_args()

    em._load_dotenv()
    chain = em.build_chains(args.backend)[1]  # stage-2 deontic extractor

    for s in args.set:
        name, runs_spec = s.split(":", 1)
        run_files = discover_runs(runs_spec)
        if not run_files:
            ap.error(f"no run files for set {name} at {runs_spec}")
        run_set(name, run_files, args.k, chain)


if __name__ == "__main__":
    main()
