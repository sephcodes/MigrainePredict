#!/usr/bin/env python3
"""
make_verification_worksheet.py -- build the Stage 3 human-review worksheet.

Reads the verdict JSONL from verify_statements.py and emits a worksheet in the
content_map_reviewed style: one row per pair needing a human label. Included:

  - every pair the detectors surfaced (verdict != 'none'), and
  - the near-miss negatives (verdict == 'none' but with partial evidence:
    linked, or one of predicate/object overlap) as labelled negatives,

so detector precision AND the plausible-recall boundary are both measurable.
Rows carry the statement anchors (pulled from the graph) so pairs can be
judged without opening the source runs.

`human_label` = what detector_verdict SHOULD have been, drawn from the SAME
value space as detector_verdict, restricted to the row's check.

LABELLING STANDARD (contradiction check): PRIMA FACIE, pair-local. Label
candidate_contradiction when the act obliged/permitted by one statement falls,
on the face of the two anchors+conditions alone, within the class prohibited
by the other. Do NOT try to judge whether some other provision resolves the
tension corpus-wide -- that is a graph computation, not a human judgment: a
resolution pass (planned addition to verify_statements.py, so it runs BEFORE
this worksheet is generated) will auto-classify a tension pair as
resolved-via-exception when its prohibition carries a covering EXCEPTION_OF
derogation, leaving only unresolved tensions in the review queue. The human
label records the pair-local tension either way; resolution only affects
routing. Scope-disjointness that is visible on the statement's own face
(e.g. Art 21(1) limited to 6(1)(e)/(f)-based processing) DOES justify none.

Value spaces:

  check=contradiction     none | exception_structure | candidate_contradiction
  check=redundancy        none | duplicate_candidate | duplicate_definition | specialisation
  check=cross_regulation  none | conflict

Scoring is then direct: agreement = (human_label == detector_verdict); per-class
P/R over the worksheet's pair pool. Nuance goes in `notes` (e.g. the Art 30(5)
condition-context reference is labelled none, with the reasoning in notes).
Existing labels in the output file are preserved on re-run (status-based HITL,
as in the mapping stage).

Usage:
  python make_verification_worksheet.py
  python make_verification_worksheet.py --verdicts PATH --out PATH
"""
import argparse
import json
import os

from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "MigrainePredict")

DEFAULT_VERDICTS = "data/verification/run4.verification.jsonl"
DEFAULT_OUT = "data/verification/verification_reviewed.json"


def near_miss(v):
    ev = v.get("evidence") or {}
    return bool(ev.get("linked") or ev.get("pred_overlap")
                or ev.get("obj_overlap") or ev.get("structural_chapeau")
                or ev.get("pred_equal") or ev.get("obj_equal"))


def assign_hubs(out):
    """Mark hub statements: one appearing in >=3 rows of the same
    (check, detector_verdict) group, so one human judgment covers the set."""
    from collections import Counter
    freq = Counter()
    for r in out:
        if r["detector_verdict"] == "none":
            continue
        key = (r["check"], r["detector_verdict"])
        freq[(key, r["a"])] += 1
        freq[(key, r["b"])] += 1
    for r in out:
        key = (r["check"], r["detector_verdict"])
        cands = [(freq[(key, s)], s) for s in (r["a"], r["b"])
                 if freq[(key, s)] >= 3]
        r["hub"] = max(cands)[1] if cands else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", default=DEFAULT_VERDICTS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    verdicts = [json.loads(l) for l in open(args.verdicts)]
    rows = [v for v in verdicts if v["verdict"] != "none"
            or near_miss(v)]

    ids = sorted({v["a"] for v in rows} | {v["b"] for v in rows})
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as sess:
        recs = sess.run(
            "MATCH (s:Statement) WHERE s.statement_id IN $ids "
            "RETURN s.statement_id AS id, s.anchor AS anchor, "
            "       s.modality AS modality, s.condition_text AS cond",
            ids=ids)
        info = {r["id"]: {"anchor": r["anchor"],
                          "condition": (r["cond"] or [None])[0]}
                for r in recs}
    driver.close()

    # preserve existing human labels (match on check + pair)
    existing = {}
    if os.path.exists(args.out):
        for row in json.load(open(args.out)):
            if row.get("human_label"):
                existing[(row["check"], row["a"], row["b"])] = (
                    row["human_label"], row.get("notes", ""))

    out = []
    for v in rows:
        key = (v["check"], v["a"], v["b"])
        label, notes = existing.get(key, ("", ""))
        out.append({
            "check": v["check"],
            "detector_verdict": v["verdict"],
            "a": v["a"], "b": v["b"],
            "a_anchor": info.get(v["a"], {}).get("anchor"),
            "b_anchor": info.get(v["b"], {}).get("anchor"),
            "a_condition": info.get(v["a"], {}).get("condition"),
            "b_condition": info.get(v["b"], {}).get("condition"),
            "evidence": v.get("evidence"),
            "pattern": v.get("pattern"),
            "synthetic": bool(v.get("synthetic")
                              or v["a"].startswith("syn:")
                              or v["b"].startswith("syn:")),
            "human_label": label,
            "notes": notes,
        })

    assign_hubs(out)
    out.sort(key=lambda r: (r["check"], r["hub"] or "", r["a"], r["b"]))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w"), indent=2)
    surfaced = sum(1 for r in out if r["detector_verdict"] != "none")
    kept = len(existing)
    print(f"worksheet: {len(out)} rows ({surfaced} surfaced, "
          f"{len(out) - surfaced} near-miss negatives, "
          f"{kept} existing labels preserved) -> {args.out}")


if __name__ == "__main__":
    main()
