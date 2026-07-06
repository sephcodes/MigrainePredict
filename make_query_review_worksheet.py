#!/usr/bin/env python3
"""
make_query_review_worksheet.py -- build the Phase 2 query-review worksheet.

Reads the GraphRAG review queue (INSUFFICIENT verdicts routed there by
graphrag_query.py) and emits a worksheet in the content_map_reviewed /
verification_reviewed style: one row per query, system output shown read-only,
human judgment fields alongside. The reviewed rows double as gold answers --
the same record shape the evaluation harness grades against (report section
5.5, human-annotation artefact (ii): a 4-label verdict plus an explanation
trace per query) -- so review work is never throwaway.

Two SEPARATE judgments per row (the S3 smoke finding: verdict label and
explanation quality can diverge -- correct label reached with a deficient
explanation):

  human_verdict            what the verdict SHOULD be, same value space as
                           the system: COMPLIANT | NON_COMPLIANT |
                           INSUFFICIENT | NOT_APPLICABLE
  human_explanation        the gold explanation trace, plain English, citing
                           statement ids in square brackets where helpful
  explanation_assessment   judges the SYSTEM's explanation against the gold:
                           correct | partly | wrong
  notes                    plain-English nuance (probe-design lessons,
                           hallucinated sentences, over-readings)
  label_source             claude_proposed (pending review) | human (final);
                           stripped-to-human on adoption, as in the
                           verification worksheet

Rows with any human field filled (or label_source set) are preserved on
re-run (status-based HITL, mapping/verification-stage pattern). Queue entries
sharing a query_id keep only the latest run.

Usage:
  python make_query_review_worksheet.py
  python make_query_review_worksheet.py --queue PATH --out PATH
"""
import argparse
import json
import os

DEFAULT_QUEUE = "data/graphrag/review_queue.jsonl"
DEFAULT_OUT = "data/graphrag/query_review.json"

HUMAN_FIELDS = ("human_verdict", "human_explanation",
                "explanation_assessment", "notes", "label_source")


def load_queue(path):
    """Latest queue entry per query_id, in first-seen order."""
    latest = {}
    order = []
    for line in open(path):
        rec = json.loads(line)
        qid = rec["query_id"]
        if qid not in latest:
            order.append(qid)
        if qid not in latest or rec["ts"] > latest[qid]["ts"]:
            latest[qid] = rec
    return [latest[q] for q in order]


def build_row(rec, existing):
    v = rec["verdict"]
    row = {
        "query_id": rec["query_id"],
        "question": rec["question"],
        "run_ts": rec["ts"],
        "seed_path": rec["seed_path"],
        "system_verdict": v["verdict"],
        "system_explanation": v["explanation"],
        "system_missing_information": v.get("missing_information"),
        "system_cited": v["cited"],
        "retrieved_statement_ids": rec["retrieved_statement_ids"],
        "human_verdict": "",
        "human_explanation": "",
        "explanation_assessment": "",
        "notes": "",
        "label_source": "",
    }
    prev = existing.get(rec["query_id"])
    if prev and any(prev.get(f) for f in HUMAN_FIELDS):
        for f in HUMAN_FIELDS:
            row[f] = prev.get(f, "")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=DEFAULT_QUEUE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    existing = {}
    if os.path.exists(args.out):
        for row in json.load(open(args.out)):
            existing[row["query_id"]] = row

    rows = [build_row(rec, existing) for rec in load_queue(args.queue)]
    kept = sum(1 for r in rows if any(r.get(f) for f in HUMAN_FIELDS))

    with open(args.out, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"wrote {len(rows)} rows ({kept} with existing human/proposed "
          f"judgments preserved) -> {args.out}")


if __name__ == "__main__":
    main()
