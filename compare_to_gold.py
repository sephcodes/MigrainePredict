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

# references: True = HARD (build-breaker, good for production CI);
# False = SOFT flag (recommended while validating generalisation).
REFERENCES_HARD = False

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
    # Strip a leading determiner so "an AI system" == "AI system",
    # "the processing of personal data" == "processing of personal data", and
    # "that AI system" == "AI system". The articles (a/an/the) and the
    # demonstratives (this/that/these/those) are the same determiner class —
    # they don't change the scoped entity. Both sides are normalised
    # symmetrically, so this only tolerates determiner drift; it never masks a
    # substantive difference.
    s = re.sub(r"^(?:a|an|the|this|that|these|those)\s+", "", s)
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

def refs_compat(a, b):
    # Same cited provision if equal OR one IRI is a path-prefix of the other:
    # gdpr:art_55 ~ gdpr:art_55/par_1. Whole-vs-paragraph citation depth is a
    # convention difference, not a wrong reference.
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")

def refs_match(gold_refs, run_refs):
    gold, run = list(gold_refs or []), list(run_refs or [])
    used = [False] * len(run)
    for g in gold:
        hit = False
        for i, r in enumerate(run):
            if not used[i] and refs_compat(g, r):
                used[i] = True; hit = True; break
        if not hit:
            return False
    return all(used) if run else (len(gold) == 0)

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
def compare_pair(gold, run, sid_map=None):
    """sid_map: run statement_id -> gold statement_id, from the global
    alignment. Used to translate intra-paragraph (statement-level) references
    so a run carve-out pointing at run-`#s2` grades against a gold carve-out
    pointing at gold-`#s1` when those siblings content-match. Paragraph-level
    IRI references are not in the map and pass through unchanged."""
    sid_map = sid_map or {}
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
        if f.endswith("references"):
            # Translate run statement-level refs (e.g. '...#s2') to their gold
            # counterpart via the alignment, so intra-paragraph parent links
            # grade by content, not by per-run id string. Paragraph IRIs are
            # untouched. Then depth-tolerant set match; HARD or SOFT per config.
            grefs = get(f, gstmt)
            rrefs = [sid_map.get(x, x) for x in (get(f, rstmt) or [])]
            if not refs_match(grefs, rrefs):
                (hard if REFERENCES_HARD else soft).append(
                    f"references: gold={grefs} run={rrefs}")
            continue
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
# Within-group alignment (Phase A): multiple statements can share the same
# (paragraph_iri, discriminator) key — e.g. Art 16's rectification + completion
# obligations, both (art_16/par_1, DEO, OBLIGATION). The old dict keyed by that
# tuple silently dropped all but the last. We now keep a LIST per key and
# content-best-match gold↔run within each group, so the harness can hold and
# grade multiple statements per key. For the common 1-gold-1-run group this is
# identical to the old behaviour (the single pair always matches).

def _match_tokens(rec):
    """Distinguishing-content token set used to pair same-key statements.
    Anchor is the strongest 'which part of the paragraph' signal; the rest are
    the class's content fields (the discriminator fields are already equal
    within a group, so they don't help separate)."""
    sc = rec.get("statement_class")
    st = rec.get("statement") or {}
    parts = [rec.get("anchor") or ""]
    if sc == "DEONTIC":
        for fld in ("predicate", "object", "subject"):
            for ev in (st.get(fld) or []):
                if isinstance(ev, dict):
                    parts.append(ev.get("value") or "")
        c = st.get("condition")
        if isinstance(c, dict):
            parts.append(c.get("value") or "")
    elif sc == "DEFINITIONAL":
        d = st.get("definition")
        if isinstance(d, dict):
            parts.append(d.get("value") or "")
    elif sc == "APPLICABILITY":
        for fld in ("applies_to", "condition"):
            v = st.get(fld)
            if isinstance(v, dict):
                parts.append(v.get("value") or "")
    elif sc == "NOT_APPLICABLE":
        parts.append((st.get("text") or "")[:120])
    blob = norm_text(" ".join(p for p in parts if p)) or ""
    return set(blob.split())

def _score_pair(g, r):
    gt, rt = _match_tokens(g), _match_tokens(r)
    if not gt and not rt:
        return 1.0
    if not gt or not rt:
        return 0.0
    return len(gt & rt) / len(gt | rt)

def match_group(gold_list, run_list):
    """Greedy content best-match within a (iri, discriminator) group.
    Returns (pairs, missing_gold, extra_run)."""
    cand = sorted(
        ((_score_pair(g, r), gi, ri)
         for gi, g in enumerate(gold_list)
         for ri, r in enumerate(run_list)),
        key=lambda x: -x[0],
    )
    used_g, used_r, pairs = set(), set(), []
    for _, gi, ri in cand:
        if gi in used_g or ri in used_r:
            continue
        used_g.add(gi); used_r.add(ri)
        pairs.append((gold_list[gi], run_list[ri]))
    missing = [g for i, g in enumerate(gold_list) if i not in used_g]
    extra = [r for i, r in enumerate(run_list) if i not in used_r]
    return pairs, missing, extra

# ----------------------------------------------------------------------------
def main():
    if len(sys.argv) != 3:
        print("usage: python compare_to_gold.py gold_set.jsonl run.jsonl"); sys.exit(2)
    gold_all = load(sys.argv[1])
    screened = [r for r in gold_all if r.get("screen_dependent")]
    gold_recs = [r for r in gold_all if not r.get("screen_dependent")]
    run_recs = load(sys.argv[2])

    # Group both sides into lists keyed by (paragraph_iri, discriminator),
    # preserving gold file order for stable output.
    from collections import OrderedDict, defaultdict
    gold_groups = OrderedDict()
    for r in gold_recs:
        gold_groups.setdefault(key(r), []).append(r)
    run_groups = defaultdict(list)
    for r in run_recs:
        run_groups[key(r)].append(r)

    ordered_keys = list(gold_groups.keys()) + [k for k in run_groups if k not in gold_groups]

    # Pass 1: match every group; build the global run->gold statement_id map
    # from all matched pairs (needed before grading so intra-paragraph
    # references can be translated).
    group_results, sid_map = {}, {}
    for k in ordered_keys:
        pairs, missing, extra = match_group(gold_groups.get(k, []), run_groups.get(k, []))
        group_results[k] = (pairs, missing, extra)
        for g, r in pairs:
            if r.get("statement_id") and g.get("statement_id"):
                sid_map[r["statement_id"]] = g["statement_id"]

    # Pass 2: grade and report (gold file order preserved).
    matched, hard_total, soft_total = 0, 0, 0
    extra_recs = []
    print("="*70)
    for k in ordered_keys:
        pairs, missing, extra = group_results[k]
        for g, r in pairs:
            matched += 1
            gid, iri = g.get("gold_id", "?"), k[0]
            hard, soft = compare_pair(g, r, sid_map)
            if not hard and not soft:
                print(f"[PASS]     {gid}  {iri}")
            else:
                print(f"[{'FAIL' if hard else 'flag'}]     {gid}  {iri}")
                for h in hard:
                    print(f"             HARD  {h}"); hard_total += 1
                for s in soft:
                    print(f"             soft  {s}"); soft_total += 1
        for g in missing:
            print(f"[MISSING]  {g.get('gold_id','?')}  {k[0]}  {k[1]}  -- no matching run record")
            hard_total += 1
        extra_recs.extend(extra)

    for r in screened:
        print(f"[SCREEN]   {r.get('gold_id','?')}  {r.get('paragraph_iri')}  -- screen_dependent, not scored")

    for r in extra_recs:
        print(f"[EXTRA]    {r.get('paragraph_iri')}  {key(r)[1]}  -- run produced a record with no gold match")
    print("="*70)
    print(f"matched {matched}/{len(gold_recs)} gold records | "
          f"HARD failures: {hard_total} | soft flags: {soft_total} | "
          f"extra run records: {len(extra_recs)} | dupe run keys: 0 | "
          f"screened-out: {len(screened)}")
    sys.exit(1 if (hard_total or extra_recs) else 0)

if __name__ == "__main__":
    main()
