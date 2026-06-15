#!/usr/bin/env python3
"""
compare_to_gold.py  --  regression harness for the extraction pipeline.

Usage:
    python compare_to_gold.py gold_set.jsonl run.jsonl

What it does
------------
Loads the frozen gold reference and a fresh extractor run, aligns records by
paragraph_iri (+ a within-paragraph discriminator), and diffs them field by field.

Two field tiers (see gold_annotation_notes.md for the rationale):

  OBJECTIVE  -> there is a single correct answer. Mismatch = HARD FAIL.
               These are what catch truncations, modality flips, wrong IRIs,
               dropped references, etc.

  INTERPRETIVE -> reasonable annotators (or wording) may differ. Mismatch =
                  SOFT FLAG for human review, not a failure. The gold value is
                  *your adjudicated convention*; deviation is surfaced so drift
                  is visible, but it does not break the build.

  IGNORED    -> model-side signals that are not ground truth (confidence;
                needs_review except where gold asserts a deterministic gate
                outcome; rationale/anchor are provenance, not graded here).

Exit code is non-zero if any HARD FAIL or any unmatched record is found, so this
can sit in a pre-commit hook or CI step.
"""
import json, re, sys, unicodedata

# ----------------------------------------------------------------------------
# Field configuration
# ----------------------------------------------------------------------------
# Top-level objective fields (present on every record)
OBJECTIVE_TOP = ["statement_class"]

# Objective fields *inside* statement, by statement_class
OBJECTIVE_STMT = {
    "DEFINITIONAL":  ["term", "definition.value", "source_article", "references"],
    "APPLICABILITY": ["scope_type", "applies_to.value", "polarity", "source_article", "references"],
    "DEONTIC":       ["modality", "source_article", "references"],
    "NOT_APPLICABLE":[],
}

# Interpretive fields (soft) inside statement, by class
INTERPRETIVE_STMT = {
    "DEFINITIONAL":  ["applies_to_healthcare"],
    "APPLICABILITY": ["condition.value", "applies_to_healthcare"],
    "DEONTIC":       ["subject", "predicate", "object", "condition", "beneficiary",
                      "applies_to_healthcare", "severity"],
    "NOT_APPLICABLE":[],
}

# needs_review is graded ONLY when the gold record asserts a non-null value
# (i.e. the deterministic profile-gate outcomes). Otherwise it is a gate-eval
# concern handled separately and is ignored here.
GRADE_NEEDS_REVIEW_WHEN_GOLD_SET = True

# ----------------------------------------------------------------------------
def norm_text(s):
    """Normalise free text for comparison: unicode-fold, collapse whitespace,
    strip surrounding quotes/punctuation, lowercase."""
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = " ".join(s.split()).strip().strip(".;,").lower()
    # Strip a leading determiner so "an AI system" == "AI system" and
    # "the processing of personal data" == "processing of personal data".
    # Both sides are normalised symmetrically, so this only tolerates
    # determiner drift; it never masks a substantive difference.
    s = re.sub(r"^(?:a|an|the)\s+", "", s)
    return s

def get(path, obj):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur

def norm_value(path, v):
    # references compared as a SET of strings (order-independent)
    if path.endswith("references"):
        return frozenset(v or [])
    # free-text-ish fields: normalise
    return norm_text(v) if isinstance(v, str) else v

def ev_list_summary(lst):
    """Summarise a list[ExtractedValue] as normalised 'value(method)' tuples."""
    if not isinstance(lst, list):
        return lst
    return [(norm_text(e.get("value")), e.get("method")) for e in lst]

# ----------------------------------------------------------------------------
def load(path):
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out

def discriminator(rec):
    """A within-paragraph key so multiple statements per IRI align correctly."""
    sc = rec.get("statement_class")
    st = rec.get("statement") or {}
    if sc == "DEFINITIONAL":
        return ("DEF", norm_text(st.get("term")))
    if sc == "APPLICABILITY":
        return ("APP", st.get("scope_type"), st.get("polarity"))
    if sc == "DEONTIC":
        return ("DEO", st.get("modality"))
    if sc == "NOT_APPLICABLE":
        # One NA per paragraph (guide §6), so align on paragraph_iri alone —
        # the NA text is the LLM-side anchor/paragraph and is not stable
        # enough to key on. A constant discriminator collapses to one slot.
        return ("NA",)
    return (sc,)

def key(rec):
    return (rec.get("paragraph_iri"), discriminator(rec))

# ----------------------------------------------------------------------------
def compare_pair(gold, run):
    hard, soft = [], []
    sc = gold.get("statement_class")

    # top-level objective
    for f in OBJECTIVE_TOP:
        if gold.get(f) != run.get(f):
            hard.append(f"{f}: gold={gold.get(f)!r} run={run.get(f)!r}")
            return hard, soft  # class mismatch: stop, the rest is meaningless

    gstmt, rstmt = gold.get("statement") or {}, run.get("statement") or {}

    # objective inside statement
    for f in OBJECTIVE_STMT.get(sc, []):
        gv, rv = norm_value(f, get(f, gstmt)), norm_value(f, get(f, rstmt))
        if gv != rv:
            hard.append(f"{f}: gold={gv!r} run={rv!r}")

    # interpretive inside statement
    for f in INTERPRETIVE_STMT.get(sc, []):
        if f in ("subject", "predicate", "object"):
            gv, rv = ev_list_summary(gstmt.get(f)), ev_list_summary(rstmt.get(f))
        elif f in ("condition", "beneficiary"):
            g_, r_ = gstmt.get(f), rstmt.get(f)
            gv = norm_text(g_.get("value")) if isinstance(g_, dict) else g_
            rv = norm_text(r_.get("value")) if isinstance(r_, dict) else r_
        else:
            gv, rv = norm_value(f, get(f, gstmt)), norm_value(f, get(f, rstmt))
        if gv != rv:
            soft.append(f"{f}: gold={gv!r} run={rv!r}")

    # needs_review (only when gold asserts it)
    if GRADE_NEEDS_REVIEW_WHEN_GOLD_SET and gold.get("needs_review") is not None:
        if gold.get("needs_review") != run.get("needs_review"):
            hard.append(f"needs_review: gold={gold.get('needs_review')} run={run.get('needs_review')}")

    return hard, soft

# ----------------------------------------------------------------------------
def main():
    if len(sys.argv) != 3:
        print("usage: python compare_to_gold.py gold_set.jsonl run.jsonl"); sys.exit(2)
    gold = {key(r): r for r in load(sys.argv[1])}
    run_recs = load(sys.argv[2])
    run = {}
    dupes = []
    for r in run_recs:
        k = key(r)
        if k in run: dupes.append(k)
        run[k] = r

    matched, hard_total, soft_total = 0, 0, 0
    print("="*70)
    for k, g in gold.items():
        gid = g.get("gold_id", "?")
        iri = k[0]
        if k not in run:
            print(f"[MISSING]  {gid}  {iri}  {k[1]}  -- no matching run record")
            hard_total += 1
            continue
        matched += 1
        hard, soft = compare_pair(g, run[k])
        if not hard and not soft:
            print(f"[PASS]     {gid}  {iri}")
        else:
            tag = "FAIL" if hard else "flag"
            print(f"[{tag}]     {gid}  {iri}")
            for h in hard:
                print(f"             HARD  {h}"); hard_total += 1
            for s in soft:
                print(f"             soft  {s}"); soft_total += 1

    # run records with no gold match (e.g. NOT_APPLICABLE duplication regressions)
    extra = [k for k in run if k not in gold]
    for k in extra:
        print(f"[EXTRA]    {k[0]}  {k[1]}  -- run produced a record with no gold match")
    print("="*70)
    print(f"matched {matched}/{len(gold)} gold records | "
          f"HARD failures: {hard_total} | soft flags: {soft_total} | "
          f"extra run records: {len(extra)} | dupe run keys: {len(dupes)}")
    sys.exit(1 if (hard_total or extra or dupes) else 0)

if __name__ == "__main__":
    main()
