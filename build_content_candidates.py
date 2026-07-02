"""Step 1 of content (predicate/object/condition) mapping: the MATCHER.

For each distinct (slot, regulation, value) in the extractions, matched only
against its routed vocabulary (mapping/slot_routing.json over terms.json).

Matching signals:
  - EXACT: whole normalised phrase equals a label (object/condition), or any
    verb-lemma token equals a single-token processing label (predicate)
    -> status="mapped", iri prefilled (spot-check).
  - LEXICAL (genuine concept mention): a vocab label whose NOUN-LEMMATISED tokens
    are fully contained in the value's tokens. Scored by summed IDF over the
    routed label corpus (so generic high-frequency tokens like "data"/"risk"
    score low), and SUBSUMED labels are dropped (Data removed when PersonalData
    also matches -> prefer the most specific). -> status="review".
  - EMBEDDING (BGE cosine top-k): a NON-AUTHORITATIVE hint only. Over a closed,
    homogeneous vocabulary the nearest term is always ~0.65-0.80 even when the
    answer is "none", so an embedding score is NOT a mappability signal and never
    sets a non-literal default. Kept under _candidates for the adjudicator.

Default disposition when there is NO lexical hit:
  - predicate / condition -> "literal" (residue: compliance verbs, qualifiers).
  - object                -> "review" (objects are what the duty acts on; an
    adjudicator should look, with lexical_hit=false flagged).
  - slot with no routed vocab (predicate.aiact) -> "no_target".

Writes the adjudication worksheet mapping/content_map.json. No record mutation,
no coverage -- that comes after you adjudicate.

Usage:
  python build_content_candidates.py data/dev_5run_deontic_pred data/holdout_5run_redundant_neg
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict

from compare_to_gold import norm_text
from predicate_norm import normalise_predicate, _LEM

HERE = os.path.dirname(__file__)
TERMS = os.path.join(HERE, "mapping", "vocab", "terms.json")
ROUTING = os.path.join(HERE, "mapping", "slot_routing.json")
SYNONYMS = os.path.join(HERE, "mapping", "predicate_synonyms.json")
OBJECT_ALIASES = os.path.join(HERE, "mapping", "object_aliases.json")
OUT = os.path.join(HERE, "mapping", "content_map.json")
MODEL_NAME = "BAAI/bge-small-en-v1.5"
TOPK = 3

try:
    from nltk.corpus import stopwords
    STOP = set(stopwords.words("english"))
except Exception:
    STOP = {"the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "with",
            "by", "his", "her", "its", "their", "which", "that", "such", "as"}

# A processing verb auto-maps only when it is the predicate HEAD -- i.e. it is not
# governing a separate noun object. The cheap deterministic test: the token right
# after the matched verb is not a determiner/possessive (so "use a single assessment"
# is governed -> review; "erase without undue delay" / "...to provide" are head -> map).
DETERMINERS = {"a", "an", "the", "this", "that", "these", "those", "each", "every",
               "any", "all", "no", "one", "its", "his", "her", "their", "our", "your", "my"}

EDIT_DOC = ("Adjudication worksheet. For each value set 'status' to: "
            "'mapped' (fill 'iri' with one or more vocab IRIs), "
            "'flag' (mappable content but no vocab home -> coverage MISS / HITL), "
            "'literal' (qualifier/residue, excluded from the coverage denominator), "
            "'manually_mapped' / 'manually_literal' / 'manually_flag' (LOCKED human "
            "decisions: any status prefixed 'manually_' is PRESERVED verbatim on every "
            "re-run -- the matcher never overwrites its status/iri. Use manually_mapped "
            "(fill iri) for a mapping you want kept, manually_literal for residue you've "
            "decided stays literal, manually_flag for a confirmed gap. Plain "
            "mapped/review/literal/flag are matcher output and WILL regenerate). "
            "DEFAULTS: status='mapped' = exact lexical hit (spot-check); "
            "status='review' = a genuine lexical concept mention was found "
            "(pick IRI(s) from _candidates); status='literal' = predicate/condition "
            "with no lexical concept mention (residue by default -- promote to "
            "'mapped'/'flag' only if a real concept is present). "
            "IMPORTANT: '_candidates' marked method='embed' are WEAK hints only -- "
            "BGE returns a 'nearest' term over the closed vocab even when the right "
            "answer is none, so an embed score is NOT evidence of mappability; rely "
            "on lexical hits. 'lexical_hit' flags whether any concept token matched.")


def vlem(tok):
    return _LEM.lemmatize(tok, "v")


def lemtoks(s):
    """noun-lemmatised, stopword-stripped token set of a normalised string."""
    return {_LEM.lemmatize(t) for t in norm_text(s or "").split() if t not in STOP}


def load_targets():
    terms = json.load(open(TERMS))
    routing = json.load(open(ROUTING))
    exclude = set(routing.get("_exclude_abstract", []))  # abstract roots: never content hits
    targets = {}
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            picked = {}
            for sel in routing[slot][reg]:
                voc = terms[sel["vocab"]]
                if sel["by"] == "all":
                    picked.update({c: r["label"] for c, r in voc.items()})
                elif sel["by"] == "scheme":
                    want = set(sel["values"])
                    picked.update({c: r["label"] for c, r in voc.items() if r["scheme"] in want})
                elif sel["by"] == "root":
                    want = set(sel["values"])
                    picked.update({c: r["label"] for c, r in voc.items() if r.get("root") in want})
            for c in exclude:
                picked.pop(c, None)
            targets[(slot, reg)] = picked
    return targets


def load_synonyms():
    """verb-lemma -> [IRI]; hand-curated processing-op aliases (predicate.gdpr)."""
    raw = json.load(open(SYNONYMS)).get("aliases", {})
    return {vlem(k): v for k, v in raw.items()}


def load_object_aliases():
    """surface-phrase -> [IRI]; hand-curated object aliases (surface != vocab label)."""
    return json.load(open(OBJECT_ALIASES)).get("aliases", {})


def object_alias_hits(value_lemmas, aliases, label_map, label_lemmas, idf):
    """alias phrases contained in the value -> (curie, IDF-score), treated as lexical."""
    out = []
    for phrase, iris in aliases.items():
        if lemtoks(phrase) <= value_lemmas:
            for c in iris:
                if c in label_map:
                    out.append((c, round(sum(idf.get(t, 1.0) for t in label_lemmas.get(c, set())), 3)))
    return out


def collect_values(paths):
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, "*.extracted.jsonl"))) if os.path.isdir(p) else [p]
    if not files:
        sys.exit("no .extracted.jsonl found")
    vals = defaultdict(Counter)
    for f in files:
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("statement_class") != "DEONTIC":
                continue
            st = r["statement"]
            src = st.get("source_article") or ""
            reg = src.split(":")[0]
            art = src.split("/")[0]   # article root, e.g. gdpr:art_12 (merge key for load-back)
            mod = st.get("modality")
            for p in (st.get("predicate") or []):
                v, _ = normalise_predicate(p.get("value") or "", mod)
                if v:
                    vals[("predicate", reg)][(v, art)] += 1
            for o in (st.get("object") or []):
                if o.get("value"):
                    vals[("object", reg)][(o["value"], art)] += 1
            cond = st.get("condition")
            if isinstance(cond, dict) and cond.get("value"):
                vals[("condition", reg)][(cond["value"], art)] += 1
    return vals


def build_idf(label_map):
    docs = {c: lemtoks(lab) for c, lab in label_map.items()}
    N = len(docs)
    df = Counter()
    for toks in docs.values():
        for t in toks:
            df[t] += 1
    idf = {t: math.log((N + 1) / (df_t + 1)) + 1 for t, df_t in df.items()}
    return docs, idf


def lexical_candidates(value_lemmas, label_lemmas, idf):
    """labels fully contained in the value, IDF-scored, subsumed labels dropped."""
    hits = [(c, lt, sum(idf.get(t, 1.0) for t in lt))
            for c, lt in label_lemmas.items() if lt and lt <= value_lemmas]
    keep = [(c, round(sc, 3)) for c, lt, sc in hits
            if not any(c2 != c and lt < lt2 for c2, lt2, _ in hits)]  # drop strict subsets
    return sorted(keep, key=lambda x: -x[1])


def preserve_manual(worksheet, prior_path):
    """Carry forward every row the user locked with a 'manually_*' status in the
    previous content_map.json, so re-running never clobbers hand adjudications.
    Matching is tolerant: a lock that carries a source_article applies only to the
    same (value, article) row (so the SAME value locked to DIFFERENT IRIs under
    different articles stays distinct); a lock with no article applies at the value
    level, across every article-row of that value (legacy / context-free locks).
    A locked value/article no longer produced this run is re-appended verbatim so
    nothing is silently lost. Returns the number of locked rows carried forward."""
    if not os.path.exists(prior_path):
        return 0
    try:
        prior = json.load(open(prior_path))
    except (json.JSONDecodeError, OSError):
        return 0
    manual = {}
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            for r in prior.get(slot, {}).get(reg, []):
                _st = str(r.get("status", ""))
                # preserve locked human decisions (manually_*) AND un-audited adjudicator
                # output (llm_suggested_*, escalated) so a matcher re-run can't wipe them
                if not (_st.startswith("manually_") or _st.startswith("llm_suggested_") or _st == "escalated"):
                    continue
                art = r.get("source_article")
                # article-specific lock if it carries one, else value-level (legacy/context-free)
                key = (slot, reg, r["value"], art) if art else (slot, reg, r["value"], None)
                manual[key] = r
    if not manual:
        return 0
    applied = set()
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            for row in worksheet.get(slot, {}).get(reg, []):
                art = row.get("source_article")
                # prefer an article-specific lock; fall back to a value-level lock
                key = (slot, reg, row["value"], art)
                if key not in manual:
                    key = (slot, reg, row["value"], None)
                if key in manual:
                    row["status"] = manual[key]["status"]   # carry the exact locked status
                    row["iri"] = manual[key].get("iri", [])
                    applied.add(key)
    for key, r in manual.items():
        if key not in applied:  # orphan: value/article not produced this run -> keep it
            slot, reg = key[0], key[1]
            worksheet.setdefault(slot, {}).setdefault(reg, []).append(r)
    return len(manual)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="run dirs of *.extracted.jsonl")
    args = ap.parse_args()

    targets = load_targets()
    synonyms = load_synonyms()
    obj_aliases = load_object_aliases()
    idf_floor = json.load(open(ROUTING)).get("_idf_floor", 3.0)
    values = collect_values(args.paths)

    from sentence_transformers import SentenceTransformer
    import numpy as np
    model = SentenceTransformer(MODEL_NAME)

    def embed(texts):
        return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    worksheet = {"_doc": EDIT_DOC}
    summary = []
    for slot in ("predicate", "object", "condition"):
        worksheet[slot] = {}
        for reg in ("gdpr", "aiact"):
            label_map = targets[(slot, reg)]
            vcounter = values.get((slot, reg), Counter())
            rows, counts = [], Counter()

            if not label_map:  # structural gap
                for (v, art), n in vcounter.most_common():
                    rows.append({"value": v, "source_article": art, "count": n, "status": "no_target",
                                 "lexical_hit": False, "iri": [], "_candidates": []})
                    counts["no_target"] += 1
                worksheet[slot][reg] = rows
                summary.append((slot, reg, len(rows), dict(counts)))
                continue

            label_lemmas, idf = build_idf(label_map)
            by_phrase = {}
            by_vlemma = defaultdict(list)
            for c, lab in label_map.items():
                by_phrase.setdefault(norm_text(lab or ""), c)
                lt = label_lemmas[c]
                if len(lt) == 1:
                    by_vlemma[vlem(next(iter(lt)))].append(c)
            curies = list(label_map)
            lab_emb = embed([label_map[c] for c in curies])

            for (v, art), n in vcounter.most_common():
                vn = norm_text(v)
                vlemmas = lemtoks(v)
                ptoks = vn.split()

                # head verb lemmas (predicate): a verb token NOT immediately
                # governing a determiner-led noun object
                head_lemmas = set()
                if slot == "predicate":
                    for i, t in enumerate(ptoks):
                        nxt = ptoks[i + 1] if i + 1 < len(ptoks) else None
                        if nxt is None or nxt not in DETERMINERS:
                            head_lemmas.add(vlem(t))

                # exact: whole-phrase label equality; (predicate) head verb == single-token label
                exact = [by_phrase[vn]] if vn in by_phrase else []
                if slot == "predicate":
                    exact = list(dict.fromkeys(exact + [c for lem in head_lemmas for c in by_vlemma.get(lem, [])]))
                if exact:
                    rows.append({"value": v, "source_article": art, "count": n, "status": "mapped", "lexical_hit": True,
                                 "iri": exact,
                                 "_candidates": [{"iri": c, "label": label_map[c], "method": "exact", "score": 1.0} for c in exact]})
                    counts["mapped"] += 1
                    continue
                # hand-curated synonym aliases (predicate.gdpr), head-gated
                if slot == "predicate":
                    syn = [c for lem in head_lemmas for c in synonyms.get(lem, []) if c in label_map]
                    syn = list(dict.fromkeys(syn))
                    if syn:
                        rows.append({"value": v, "source_article": art, "count": n, "status": "mapped", "lexical_hit": True,
                                     "iri": syn,
                                     "_candidates": [{"iri": c, "label": label_map[c], "method": "synonym", "score": 1.0} for c in syn]})
                        counts["mapped"] += 1
                        continue
                # lexical concept mentions (IDF-scored, subsumed); objects also pull
                # hand-curated surface aliases (treated as lexical hits)
                lex = lexical_candidates(vlemmas, label_lemmas, idf)
                alias_set = set()
                if slot == "object":
                    have = {c for c, _ in lex}
                    for c, sc in object_alias_hits(vlemmas, obj_aliases, label_map, label_lemmas, idf):
                        if c not in have:
                            lex.append((c, sc))
                            have.add(c)
                            alias_set.add(c)
                    lex.sort(key=lambda x: -x[1])
                cand = [{"iri": c, "label": label_map[c],
                         "method": "alias" if c in alias_set else "lexical", "score": sc} for c, sc in lex]
                have = {c["iri"] for c in cand}
                sims = embed([v])[0] @ lab_emb.T
                for i in np.argsort(-sims)[:TOPK]:
                    c = curies[int(i)]
                    if c not in have:
                        cand.append({"iri": c, "label": label_map[c], "method": "embed",
                                     "score": round(float(sims[int(i)]), 3)})
                cand = cand[:6]

                # disposition
                if slot == "object":
                    # SIMPLE RULE: map every concept whose score clears the floor
                    # (multi-concept objects map to all of them); if nothing
                    # clears the floor, send to review. No head/headedness test --
                    # "the personal data breach" -> [PersonalData], breach is residue.
                    strong = [c for c, sc in lex if sc >= idf_floor]
                    if strong:
                        rows.append({"value": v, "source_article": art, "count": n, "status": "mapped", "lexical_hit": True,
                                     "iri": strong, "_candidates": cand})
                        counts["mapped"] += 1
                        continue
                    status = "review"      # no lexical hit clears the floor -> adjudicate
                elif lex:
                    status = "review"      # predicate/condition: genuine concept mention
                else:
                    status = "literal"     # predicate/condition, no lexical hit -> residue
                rows.append({"value": v, "source_article": art, "count": n, "status": status,
                             "lexical_hit": bool(lex), "iri": [], "_candidates": cand})
                counts[status] += 1

            worksheet[slot][reg] = rows
            summary.append((slot, reg, len(rows), dict(counts)))

    # preserve hand adjudications (status='manually_mapped') from the prior run,
    # read from OUT BEFORE we overwrite it
    n_manual = preserve_manual(worksheet, OUT)

    json.dump(worksheet, open(OUT, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {OUT}  (preserved {n_manual} locked/adjudicated row(s))\n")
    # recompute the summary from the merged worksheet so counts include manually_mapped
    print(f"{'slot':10s} {'reg':6s} {'distinct':8s}  disposition")
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            rows = worksheet[slot][reg]
            print(f"{slot:10s} {reg:6s} {len(rows):8d}  {dict(Counter(r['status'] for r in rows))}")


if __name__ == "__main__":
    main()