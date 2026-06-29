"""Step 1 of content (predicate/object/condition) mapping: the MATCHER.

For each distinct (slot, regulation, value) in the extractions, matched only
against its routed vocabulary (mapping/slot_routing.json over terms.json):

  - lexical EXACT/lemma hit  -> auto-mapped (status="mapped", iri prefilled)
  - otherwise                -> status="review" with SUGGESTIONS from
        (a) lexical label-token overlap and (b) BGE embedding cosine (top-k),
        never auto-accepted -- you adjudicate.
  - slot with no routed vocab (predicate.aiact) -> status="no_target".

Predicate is matched on its lemmatised token set (reusing predicate_norm), so
"erase"/"further process"/"no longer process" all reach dpv:Erase / dpv:Process*.
Object/condition exact match is on the whole normalised phrase (rare); the real
work for those is the suggestion+adjudication path.

Writes the adjudication worksheet mapping/content_map.json. It does NOT mutate
records and computes no coverage -- that comes after you adjudicate.

Usage:
  python build_content_candidates.py data/dev_5run_deontic_pred data/holdout_5run_redundant_neg
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

from compare_to_gold import norm_text
from predicate_norm import normalise_predicate, _LEM

HERE = os.path.dirname(__file__)
TERMS = os.path.join(HERE, "mapping", "vocab", "terms.json")
ROUTING = os.path.join(HERE, "mapping", "slot_routing.json")
OUT = os.path.join(HERE, "mapping", "content_map.json")
MODEL_NAME = "BAAI/bge-small-en-v1.5"
TOPK = 3
OVERLAP_MIN = 0.5

EDIT_DOC = ("Adjudication worksheet. For each value set 'status' to one of: "
            "'mapped' (fill 'iri' with one or more vocab IRIs), "
            "'flag' (mappable content but no vocab home -> coverage MISS / HITL), "
            "'literal' (qualifier/residue, excluded from the coverage denominator). "
            "Auto status='mapped' rows are exact lexical hits (spot-check them). "
            "'_candidates' are matcher suggestions only (lexical + embedding); "
            "they are never authoritative -- move chosen IRIs into 'iri'.")


def vlem(tok):
    return _LEM.lemmatize(tok, "v")


def load_targets():
    terms = json.load(open(TERMS))
    routing = json.load(open(ROUTING))
    targets = {}  # (slot, reg) -> {curie: label}
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            sel_list = routing[slot][reg]
            picked = {}
            for sel in sel_list:
                voc = terms[sel["vocab"]]
                if sel["by"] == "all":
                    picked.update({c: r["label"] for c, r in voc.items()})
                elif sel["by"] == "scheme":
                    want = set(sel["values"])
                    picked.update({c: r["label"] for c, r in voc.items() if r["scheme"] in want})
                elif sel["by"] == "root":
                    want = set(sel["values"])
                    picked.update({c: r["label"] for c, r in voc.items() if r.get("root") in want})
            targets[(slot, reg)] = picked
    return targets


def collect_values(paths):
    """-> {(slot, reg): Counter(value)}; predicate values are lemmatised heads."""
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
            reg = (st.get("source_article") or "").split(":")[0]
            mod = st.get("modality")
            for p in (st.get("predicate") or []):
                v, _ = normalise_predicate(p.get("value") or "", mod)
                if v:
                    vals[("predicate", reg)][v] += 1
            for o in (st.get("object") or []):
                if o.get("value"):
                    vals[("object", reg)][o["value"]] += 1
            cond = st.get("condition")
            if isinstance(cond, dict) and cond.get("value"):
                vals[("condition", reg)][cond["value"]] += 1
    return vals


def build_label_indexes(label_map):
    """exact lookups: predicate by lemma token, object/condition by full norm phrase."""
    by_lemma = defaultdict(list)   # lemma -> [curie]   (single-token labels)
    by_phrase = {}                 # norm(label) -> curie
    tokens = {}                    # curie -> (set(label tokens), norm label)
    for c, lab in label_map.items():
        n = norm_text(lab or "")
        by_phrase.setdefault(n, c)
        toks = n.split()
        tokens[c] = (set(toks), n)
        if len(toks) == 1:
            by_lemma[vlem(toks[0])].append(c)
    return by_lemma, by_phrase, tokens


def lexical_candidates(value_norm, tokens):
    vt = set(value_norm.split())
    out = []
    for c, (ltoks, _n) in tokens.items():
        if not ltoks:
            continue
        ov = len(ltoks & vt) / len(ltoks)
        if ltoks <= vt or ov >= OVERLAP_MIN:
            out.append((c, round(ov, 3)))
    return sorted(out, key=lambda x: -x[1])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="run dirs of *.extracted.jsonl")
    args = ap.parse_args()

    targets = load_targets()
    values = collect_values(args.paths)

    # embedding model + per-(slot,reg) label embedding cache (lazy)
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
            rows = []
            counts = Counter()
            if not label_map:  # no routed vocab -> structural gap
                for v, n in vcounter.most_common():
                    rows.append({"value": v, "count": n, "status": "no_target", "iri": [], "_candidates": []})
                    counts["no_target"] += 1
                worksheet[slot][reg] = rows
                summary.append((slot, reg, len(rows), dict(counts)))
                continue

            by_lemma, by_phrase, tokens = build_label_indexes(label_map)
            curies = list(label_map)
            lab_emb = embed([label_map[c] for c in curies]) if curies else None

            for v, n in vcounter.most_common():
                vn = norm_text(v)
                # exact: phrase equality, or (predicate) any token-lemma == single-token label lemma
                exact = []
                if vn in by_phrase:
                    exact = [by_phrase[vn]]
                if slot == "predicate":
                    vlemmas = {vlem(t) for t in vn.split()}
                    exact = list(dict.fromkeys(exact + [c for lem in vlemmas for c in by_lemma.get(lem, [])]))
                if exact:
                    rows.append({"value": v, "count": n, "status": "mapped", "iri": exact,
                                 "_candidates": [{"iri": c, "label": label_map[c], "method": "exact", "score": 1.0} for c in exact]})
                    counts["mapped"] += 1
                    continue
                # suggestions: lexical overlap + embedding top-k
                cand = {}
                for c, ov in lexical_candidates(vn, tokens):
                    cand[c] = {"iri": c, "label": label_map[c], "method": "lexical", "score": ov}
                if lab_emb is not None:
                    sims = embed([v])[0] @ lab_emb.T
                    for i in np.argsort(-sims)[:TOPK]:
                        c = curies[int(i)]
                        s = round(float(sims[int(i)]), 3)
                        if c not in cand or s > cand[c]["score"]:
                            cand[c] = {"iri": c, "label": label_map[c], "method": "embed", "score": s}
                clist = sorted(cand.values(), key=lambda x: -x["score"])[:5]
                rows.append({"value": v, "count": n, "status": "review", "iri": [], "_candidates": clist})
                counts["review"] += 1
            worksheet[slot][reg] = rows
            summary.append((slot, reg, len(rows), dict(counts)))

    json.dump(worksheet, open(OUT, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {OUT}\n")
    print(f"{'slot':10s} {'reg':6s} {'distinct':8s}  disposition")
    for slot, reg, tot, c in summary:
        print(f"{slot:10s} {reg:6s} {tot:8d}  {c}")


if __name__ == "__main__":
    main()
