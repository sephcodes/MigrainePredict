#!/usr/bin/env python3
"""
verify_statements.py -- Stage 3 verification: three Cypher checks over :Candidate.

Checks (parameterised Cypher pattern-matches, per interim report 5.3.1/5.4),
after the first-round evaluation against the labelled worksheet (all rules
below are grounded in that data):

  1. Contradiction / exception-structure. Opposite-polarity DEONTIC pairs
     sharing a subject concept.
       exception_structure: pair contains a PERMISSION/DISPENSATION, is linked
         (statement REFERS_TO, provision REFERS_TO, or same paragraph) AND is
         same-article (all true exceptions in gold are same-article; the one
         cross-article link, Art 30(5)->9(1), is condition-context, not
         derogation). -> :EXCEPTION_OF (derogator -> norm).
       candidate_contradiction (prima facie tension), unlinked pairs, two rules:
         (a) predicate AND object overlap where at least one overlap witness
             (the BROADER ancestor) is DISCRIMINATIVE -- i.e. not in the
             generic tier (concepts on >= GENERIC_FRACTION of deontic
             candidates: PersonalData, Processing, etc.). Generic-only overlap
             was 20/20 false alarms in round 1.
         (b) an UNCONDITIONAL prohibition (empty condition) vs an obligation/
             permission whose object concepts subsume or are subsumed by the
             prohibition's object class (the Art 9(1) family -- round 1's
             four misses; predicate mapping not required, since literal
             predicates caused the misses).
       Resolution pass: a tension whose prohibition side carries >= 1 incoming
       :EXCEPTION_OF derogation is marked resolved_via_exception and does NOT
       enter the review queue (prima facie label unchanged; routing only).
       -> :CANDIDATE_CONTRADICTION {resolved} edge; only unresolved flag.

  2. Redundancy / specialisation.
       specialisation: STRUCTURAL -- same modality, shared subject, and one
         statement's paragraph is a pt_* child of the other's (chapeau ->
         sub-point). In gold this is 4/4 with 0 FPs; the round-1 concept-
         subsumption rule produced sibling-pair artifacts (0.38 precision)
         and missed pt_c. -> :SPECIALISES (informational).
       duplicate_candidate: same modality + shared subject + same article
         root + equal object concept-sets + equal predicate concept-sets +
         compatible conditions (both empty, or content-token Jaccard >=
         DUP_COND_JACCARD). Round 1's three all-FP duplicates each fail one
         of the added terms. duplicate_definition: same term, two DEFINITIONAL
         statements. -> :REDUNDANT_WITH (flagged).

  3. Cross-regulation conflict. Curated pattern table (unchanged); flagship
     logging-vs-storage-limitation demonstrated on the injected :Synthetic
     pair. -> :CONFLICTS_WITH + flag.

Outcome: candidates get verification_status 'verified' or 'flagged' (flagged =
unresolved tension, duplicate, or conflict member; held out of auto-ingest per
FR5). Verdicts -> replayable JSONL. Idempotent re-runs.

Usage:
  python verify_statements.py                 # inject synthetic pair, run checks
  python verify_statements.py --no-synthetic  # real statements only
  python verify_statements.py --out PATH      # verdict JSONL path
"""
import argparse
import datetime
import json
import os
import re

from neo4j import GraphDatabase

AUDIT_LOG = "data/verification/audit_log.jsonl"


def audit(component, event, **payload):
    """NFR1: one timestamped JSONL event per pipeline action."""
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "component": component, "event": event, **payload}) + "\n")

NEO4J_URI = os.environ.get("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "MigrainePredict")

DEFAULT_OUT = "data/verification/run4.verification.jsonl"

GENERIC_FRACTION = 0.10   # concept on >=10% of deontic candidates = generic
DUP_COND_JACCARD = 0.5    # condition-compatibility floor for duplicates

POLAR_PAIRS = [
    ["OBLIGATION", "PROHIBITION"], ["PROHIBITION", "OBLIGATION"],
    ["PERMISSION", "PROHIBITION"], ["PROHIBITION", "PERMISSION"],
    ["OBLIGATION", "DISPENSATION"], ["DISPENSATION", "OBLIGATION"],
    ["PROHIBITION", "DISPENSATION"], ["DISPENSATION", "PROHIBITION"],
]
DEROGATORS = {"PERMISSION", "DISPENSATION"}

VERIFICATION_EDGES = ("EXCEPTION_OF", "CANDIDATE_CONTRADICTION",
                      "REDUNDANT_WITH", "SPECIALISES", "CONFLICTS_WITH")

CONFLICT_PATTERNS = [
    {
        "name": "logging_vs_storage_limitation",
        "description": "AI Act record-keeping/logging obligation vs GDPR "
                       "storage limitation (Art 12 AIA vs Art 5(1)(e) GDPR)",
        "a_reg": "aiact:", "a_modalities": ["OBLIGATION"],
        "a_concepts": ["vair:LoggingMeasure", "dpv:LoggingPolicy",
                       "dpv:RecordsOfActivities", "dpv:Record"],
        "b_reg": "gdpr:", "b_modalities": ["OBLIGATION", "PROHIBITION"],
        "b_concepts": ["eu-gdpr:StorageLimitationPrinciple",
                       "dpv:StorageDuration", "dpv:StorageDeletion"],
    },
]

GENERIC_QUERY = """
MATCH (st:Candidate {statement_class:'DEONTIC'}) WHERE NOT st:Synthetic
WITH count(st) AS total
MATCH (st:Candidate)-[:HAS_PREDICATE|HAS_OBJECT|HAS_CONDITION]->(c:Concept)
WHERE NOT st:Synthetic
WITH total, c.iri AS iri, count(DISTINCT st) AS n
WHERE n >= total * $fraction
RETURN collect(iri) AS generic
"""

CHECK1_QUERY = """
MATCH (a:Candidate {statement_class:'DEONTIC'}),
      (b:Candidate {statement_class:'DEONTIC'})
WHERE a.statement_id < b.statement_id
  AND [a.modality, b.modality] IN $polar
  AND EXISTS { MATCH (a)-[:HAS_SUBJECT]->(:Concept)<-[:HAS_SUBJECT]-(b) }
RETURN a.statement_id AS a_id, b.statement_id AS b_id,
       a.modality AS a_mod, b.modality AS b_mod,
       a.paragraph_iri AS a_para, b.paragraph_iri AS b_para,
       a.condition_text AS a_cond, b.condition_text AS b_cond,
       EXISTS { MATCH (a)-[:REFERS_TO]-(b) } AS ref_statement,
       EXISTS { MATCH (a)-[:REFERS_TO]->(:Provision)<-[:SOURCED_FROM]-(b) }
         AS ref_a_to_b,
       EXISTS { MATCH (b)-[:REFERS_TO]->(:Provision)<-[:SOURCED_FROM]-(a) }
         AS ref_b_to_a,
       EXISTS { MATCH (a)-[:SOURCED_FROM]->(:Provision)<-[:SOURCED_FROM]-(b) }
         AS same_paragraph,
       EXISTS { MATCH (a)-[:HAS_PREDICATE]->(pa:Concept),
                      (b)-[:HAS_PREDICATE]->(pb:Concept)
                WHERE (pa)-[:BROADER*0..]->(pb) OR (pb)-[:BROADER*0..]->(pa) }
         AS pred_overlap,
       EXISTS { MATCH (a)-[:HAS_OBJECT]->(oa:Concept),
                      (b)-[:HAS_OBJECT]->(ob:Concept)
                WHERE (oa)-[:BROADER*0..]->(ob) OR (ob)-[:BROADER*0..]->(oa) }
         AS obj_overlap,
       EXISTS { MATCH (a)-[:HAS_PREDICATE]->(pa:Concept),
                      (b)-[:HAS_PREDICATE]->(pb:Concept)
                WHERE ((pa)-[:BROADER*0..]->(pb) AND NOT pb.iri IN $generic)
                   OR ((pb)-[:BROADER*0..]->(pa) AND NOT pa.iri IN $generic) }
         AS pred_disc,
       EXISTS { MATCH (a)-[:HAS_OBJECT]->(oa:Concept),
                      (b)-[:HAS_OBJECT]->(ob:Concept)
                WHERE ((oa)-[:BROADER*0..]->(ob) AND NOT ob.iri IN $generic)
                   OR ((ob)-[:BROADER*0..]->(oa) AND NOT oa.iri IN $generic) }
         AS obj_disc
ORDER BY a_id, b_id
"""

HAS_EXCEPTION_QUERY = """
MATCH (:Statement)-[:EXCEPTION_OF]->(s:Statement {statement_id: $id})
RETURN count(*) > 0 AS has_exception
"""

CHECK2_QUERY = """
MATCH (a:Candidate {statement_class:'DEONTIC'}),
      (b:Candidate {statement_class:'DEONTIC'})
WHERE a.statement_id < b.statement_id
  AND a.modality = b.modality
  AND split(a.paragraph_iri, '/par_')[0] = split(b.paragraph_iri, '/par_')[0]
  AND EXISTS { MATCH (a)-[:HAS_SUBJECT]->(:Concept)<-[:HAS_SUBJECT]-(b) }
RETURN a.statement_id AS a_id, b.statement_id AS b_id,
       a.modality AS a_mod, b.modality AS b_mod,
       a.paragraph_iri AS a_para, b.paragraph_iri AS b_para,
       a.condition_text AS a_cond, b.condition_text AS b_cond,
       [(a)-[:HAS_OBJECT]->(c:Concept) | c.iri] AS a_obj,
       [(b)-[:HAS_OBJECT]->(c:Concept) | c.iri] AS b_obj,
       [(a)-[:HAS_PREDICATE]->(c:Concept) | c.iri] AS a_pred,
       [(b)-[:HAS_PREDICATE]->(c:Concept) | c.iri] AS b_pred
ORDER BY a_id, b_id
"""

CHECK2_DEF_QUERY = """
MATCH (a:Candidate {statement_class:'DEFINITIONAL'}),
      (b:Candidate {statement_class:'DEFINITIONAL'})
WHERE a.statement_id < b.statement_id AND toLower(a.term) = toLower(b.term)
RETURN a.statement_id AS a_id, b.statement_id AS b_id, a.term AS term
"""

CHECK3_QUERY = """
MATCH (a:Candidate {statement_class:'DEONTIC'})
WHERE a.paragraph_iri STARTS WITH $a_reg AND a.modality IN $a_mods
  AND EXISTS { MATCH (a)-[:HAS_PREDICATE|HAS_OBJECT|HAS_CONDITION]->
                     (c:Concept)-[:BROADER*0..]->(t:Concept)
               WHERE t.iri IN $a_concepts }
MATCH (b:Candidate {statement_class:'DEONTIC'})
WHERE b.paragraph_iri STARTS WITH $b_reg AND b.modality IN $b_mods
  AND EXISTS { MATCH (b)-[:HAS_PREDICATE|HAS_OBJECT|HAS_CONDITION]->
                     (c:Concept)-[:BROADER*0..]->(t:Concept)
               WHERE t.iri IN $b_concepts }
RETURN a.statement_id AS a_id, b.statement_id AS b_id,
       a.modality AS a_mod, b.modality AS b_mod,
       a.paragraph_iri AS a_para, b.paragraph_iri AS b_para
ORDER BY a_id, b_id
"""

SYNTHETIC = [
    {
        "statement_id": "syn:aiact:art_12/par_1#s1",
        "paragraph_iri": "aiact:art_12/par_1",
        "modality": "OBLIGATION", "modality_iri": "mp:hasObligation",
        "subject_iri": "airo:AIProvider",
        "predicate_text": ["technically allow for"],
        "object_text": ["automatic recording of events (logs) over the "
                        "lifetime of the system"],
        "object_iris": ["vair:LoggingMeasure"],
        "condition_iris": [],
        "condition_text": [],
    },
    {
        "statement_id": "syn:gdpr:art_5/par_1/pt_e#s1",
        "paragraph_iri": "gdpr:art_5/par_1/pt_e",
        "modality": "OBLIGATION", "modality_iri": "mp:hasObligation",
        "subject_iri": "dpv:DataController",
        "predicate_text": ["keep"],
        "object_text": ["personal data"],
        "object_iris": ["dpv:PersonalData"],
        "condition_iris": ["eu-gdpr:StorageLimitationPrinciple"],
        "condition_text": ["in a form which permits identification of data "
                           "subjects for no longer than is necessary for the "
                           "purposes for which the personal data are processed"],
    },
]

_STOP = {"the", "a", "an", "of", "for", "to", "in", "or", "and", "is", "are",
         "be", "by", "with", "which", "that", "as", "on", "shall", "not"}


def content_tokens(texts):
    toks = set()
    for t in texts or []:
        toks |= {w for w in re.findall(r"[a-z]+", (t or "").lower())
                 if w not in _STOP}
    return toks


def cond_compatible(a_cond, b_cond):
    ta, tb = content_tokens(a_cond), content_tokens(b_cond)
    if not ta and not tb:
        return True
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= DUP_COND_JACCARD


def article_root(paragraph_iri):
    return paragraph_iri.split("/par_")[0]


def is_pt_child(parent_para, child_para):
    return child_para.startswith(parent_para + "/pt_")


def unconditional(cond_text):
    return not any((c or "").strip() for c in cond_text or [])


def inject_synthetic(sess):
    for s in SYNTHETIC:
        sess.run(
            "MERGE (s:Statement {statement_id: $id}) "
            "SET s:Candidate:Synthetic, s.statement_class = 'DEONTIC', "
            "    s.eval_set = 'SYNTHETIC', s.modality = $mod, "
            "    s.modality_iri = $mod_iri, s.paragraph_iri = $para, "
            "    s.source_article = $para, s.needs_review = false, "
            "    s.predicate_text = $pred_text, s.object_text = $obj_text, "
            "    s.condition_text = $cond_text, s.flags = ['synthetic'] "
            "MERGE (p:Provision {iri: $para}) MERGE (s)-[:SOURCED_FROM]->(p) "
            "MERGE (c:Concept {iri: $subj}) "
            "MERGE (s)-[:HAS_SUBJECT {value: $subj, method: 'SYNTHETIC', "
            "       mapping_status: 'mapped'}]->(c)",
            id=s["statement_id"], mod=s["modality"], mod_iri=s["modality_iri"],
            para=s["paragraph_iri"], pred_text=s["predicate_text"],
            obj_text=s["object_text"], cond_text=s["condition_text"],
            subj=s["subject_iri"])
        for rel, iris in (("HAS_OBJECT", s["object_iris"]),
                          ("HAS_CONDITION", s["condition_iris"])):
            for iri in iris:
                sess.run(
                    f"MATCH (s:Statement {{statement_id: $id}}) "
                    f"MERGE (c:Concept {{iri: $iri}}) "
                    f"MERGE (s)-[:{rel} {{value: $iri, method: 'SYNTHETIC', "
                    f"       mapping_status: 'mapped'}}]->(c)",
                    id=s["statement_id"], iri=iri)


def remove_synthetic(sess):
    sess.run("MATCH (s:Synthetic) DETACH DELETE s")


def merge_edge(sess, rel, src, dst, props):
    sess.run(
        f"MATCH (a:Statement {{statement_id: $src}}), "
        f"      (b:Statement {{statement_id: $dst}}) "
        f"MERGE (a)-[r:{rel}]->(b) SET r += $props",
        src=src, dst=dst, props=props)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-synthetic", action="store_true",
                    help="remove/skip the synthetic conflict pair")
    ap.add_argument("--out", default=DEFAULT_OUT, help="verdict JSONL path")
    ap.add_argument("--reviewed",
                    default="data/verification/verification_reviewed.json",
                    help="review worksheet; human labels on flag-producing "
                         "pairs are applied as dispositions")
    args = ap.parse_args()

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    verdicts = []
    flagged = set()

    with driver.session() as sess:
        sess.run(f"MATCH ()-[r:{'|'.join(VERIFICATION_EDGES)}]->() DELETE r")
        if args.no_synthetic:
            remove_synthetic(sess)
        else:
            inject_synthetic(sess)

        generic = sess.run(GENERIC_QUERY,
                           fraction=GENERIC_FRACTION).single()["generic"]
        print(f"generic concept tier (>= {GENERIC_FRACTION:.0%} of deontic "
              f"candidates): {sorted(generic)}")

        # ---- Check 1: two passes (exceptions first, so the resolution pass
        # ---- can see the EXCEPTION_OF edges when classifying tensions) ------
        rows1 = [dict(r) for r in sess.run(CHECK1_QUERY, polar=POLAR_PAIRS,
                                           generic=generic)]
        tension_rows = []
        for r in rows1:
            linked = (r["ref_statement"] or r["ref_a_to_b"] or
                      r["ref_b_to_a"] or r["same_paragraph"])
            same_article = article_root(r["a_para"]) == article_root(r["b_para"])
            has_derogator = bool({r["a_mod"], r["b_mod"]} & DEROGATORS)
            r["_linked"], r["_same_article"] = linked, same_article

            if linked and has_derogator and same_article:
                derog, norm = ((r["a_id"], r["b_id"])
                               if r["a_mod"] in DEROGATORS
                               else (r["b_id"], r["a_id"]))
                merge_edge(sess, "EXCEPTION_OF", derog, norm,
                           {"check": "contradiction"})
                verdicts.append({
                    "check": "contradiction", "verdict": "exception_structure",
                    "a": r["a_id"], "b": r["b_id"],
                    "a_modality": r["a_mod"], "b_modality": r["b_mod"],
                    "evidence": {"linked": True, "same_article": True,
                                 "pred_overlap": r["pred_overlap"],
                                 "obj_overlap": r["obj_overlap"]},
                })
            else:
                tension_rows.append(r)

        for r in tension_rows:
            proh = (r["a_id"] if r["a_mod"] == "PROHIBITION" else
                    r["b_id"] if r["b_mod"] == "PROHIBITION" else None)
            proh_cond = (r["a_cond"] if r["a_mod"] == "PROHIBITION"
                         else r["b_cond"])
            rule_a = (not r["_linked"] and r["pred_overlap"] and r["obj_overlap"]
                      and (r["pred_disc"] or r["obj_disc"]))
            rule_b = (not r["_linked"] and proh is not None
                      and unconditional(proh_cond) and r["obj_overlap"])

            if rule_a or rule_b:
                resolved = False
                if proh is not None:
                    resolved = sess.run(HAS_EXCEPTION_QUERY,
                                        id=proh).single()["has_exception"]
                merge_edge(sess, "CANDIDATE_CONTRADICTION",
                           r["a_id"], r["b_id"],
                           {"check": "contradiction", "resolved": resolved})
                if not resolved:
                    flagged.update([r["a_id"], r["b_id"]])
                verdict = "candidate_contradiction"
            else:
                resolved = False
                verdict = "none"

            verdicts.append({
                "check": "contradiction", "verdict": verdict,
                "a": r["a_id"], "b": r["b_id"],
                "a_modality": r["a_mod"], "b_modality": r["b_mod"],
                "evidence": {
                    "linked": r["_linked"], "same_article": r["_same_article"],
                    "pred_overlap": r["pred_overlap"],
                    "obj_overlap": r["obj_overlap"],
                    "discriminative_witness": r["pred_disc"] or r["obj_disc"],
                    "proh_unconditional": (proh is not None
                                           and unconditional(proh_cond)),
                    "resolved_via_exception": resolved,
                },
            })

        # ---- Check 2: redundancy / structural specialisation ----------------
        for row in sess.run(CHECK2_QUERY):
            r = dict(row)
            oa, ob = set(r["a_obj"]), set(r["b_obj"])
            pa, pb = set(r["a_pred"]), set(r["b_pred"])
            a_parent = is_pt_child(r["a_para"], r["b_para"])
            b_parent = is_pt_child(r["b_para"], r["a_para"])
            conds_ok = cond_compatible(r["a_cond"], r["b_cond"])

            if a_parent or b_parent:
                verdict = "specialisation"
                child, parent = ((r["b_id"], r["a_id"]) if a_parent
                                 else (r["a_id"], r["b_id"]))
                merge_edge(sess, "SPECIALISES", child, parent,
                           {"check": "redundancy"})
            elif oa and oa == ob and pa == pb and conds_ok:
                verdict = "duplicate_candidate"
                merge_edge(sess, "REDUNDANT_WITH", r["a_id"], r["b_id"],
                           {"check": "redundancy"})
                flagged.update([r["a_id"], r["b_id"]])
            else:
                verdict = "none"

            verdicts.append({
                "check": "redundancy", "verdict": verdict,
                "a": r["a_id"], "b": r["b_id"],
                "a_modality": r["a_mod"], "b_modality": r["b_mod"],
                "evidence": {
                    "structural_chapeau": a_parent or b_parent,
                    "a_object_concepts": sorted(oa),
                    "b_object_concepts": sorted(ob),
                    "pred_equal": pa == pb, "obj_equal": bool(oa) and oa == ob,
                    "cond_compatible": conds_ok,
                },
            })

        for row in sess.run(CHECK2_DEF_QUERY):
            r = dict(row)
            merge_edge(sess, "REDUNDANT_WITH", r["a_id"], r["b_id"],
                       {"check": "redundancy"})
            flagged.update([r["a_id"], r["b_id"]])
            verdicts.append({
                "check": "redundancy", "verdict": "duplicate_definition",
                "a": r["a_id"], "b": r["b_id"],
                "evidence": {"term": r["term"]},
            })

        # ---- Check 3: cross-regulation conflict (curated patterns) ----------
        for pat in CONFLICT_PATTERNS:
            for row in sess.run(CHECK3_QUERY,
                                a_reg=pat["a_reg"], a_mods=pat["a_modalities"],
                                a_concepts=pat["a_concepts"],
                                b_reg=pat["b_reg"], b_mods=pat["b_modalities"],
                                b_concepts=pat["b_concepts"]):
                r = dict(row)
                merge_edge(sess, "CONFLICTS_WITH", r["a_id"], r["b_id"],
                           {"check": "cross_regulation", "pattern": pat["name"]})
                flagged.update([r["a_id"], r["b_id"]])
                verdicts.append({
                    "check": "cross_regulation", "verdict": "conflict",
                    "pattern": pat["name"],
                    "a": r["a_id"], "b": r["b_id"],
                    "a_modality": r["a_mod"], "b_modality": r["b_mod"],
                    "synthetic": r["a_id"].startswith("syn:")
                                 or r["b_id"].startswith("syn:"),
                })

        # ---- Disposition: :Verified label = what Phase 2 queries filter on --
        sess.run("MATCH (s:Candidate) "
                 "SET s.verification_status = 'verified', s:Verified")
        if flagged:
            sess.run(
                "MATCH (s:Candidate) WHERE s.statement_id IN $ids "
                "SET s.verification_status = 'flagged', s.needs_review = true "
                "REMOVE s:Verified",
                ids=sorted(flagged))

        # ---- Apply reviewed dispositions (the worksheet IS the sign-off) ----
        # A flagged statement re-enters the graph only when every pair that
        # flagged it carries a human label in the review worksheet. A label
        # matching the detector's verdict confirms the finding (the typed edge
        # stays, provenance visible); a label of 'none' overrules it (the
        # detector edge is deleted). Unlabelled rows leave the statement
        # flagged. This keeps human dispositions reproducible from repo files
        # alone: a clean load -> verify rebuild converges to the reviewed graph.
        n_dispositions = 0
        if flagged and os.path.exists(args.reviewed):
            wk = {(r.get("check"), r.get("a"), r.get("b")):
                  (r.get("human_label") or "")
                  for r in json.load(open(args.reviewed))}
            edge_for = {"cross_regulation": "CONFLICTS_WITH",
                        "contradiction": "CANDIDATE_CONTRADICTION",
                        "redundancy": "REDUNDANT_WITH"}

            def flag_producing(v):
                if v["verdict"] in ("conflict", "duplicate_candidate",
                                    "duplicate_definition"):
                    return True
                return (v["verdict"] == "candidate_contradiction" and not
                        (v.get("evidence") or {}).get("resolved_via_exception"))

            flag_pairs = [v for v in verdicts if flag_producing(v)]
            unreviewed = set()
            for v in flag_pairs:
                label = wk.get((v["check"], v["a"], v["b"]),
                               wk.get((v["check"], v["b"], v["a"]), ""))
                v["_human_label"] = label
                if not label:
                    unreviewed.update([v["a"], v["b"]])
            for v in flag_pairs:
                label = v.pop("_human_label")
                if not label:
                    continue
                if label == "none":  # human overruled the detector
                    sess.run(
                        f"MATCH (a:Candidate {{statement_id:$a}})"
                        f"-[e:{edge_for[v['check']]}]-"
                        f"(b:Candidate {{statement_id:$b}}) DELETE e",
                        a=v["a"], b=v["b"])
                    audit("verify_statements", "detector_edge_overruled",
                          a=v["a"], b=v["b"], check=v["check"],
                          human_label=label, source=args.reviewed)
                for sid in (v["a"], v["b"]):
                    if sid in unreviewed or sid not in flagged:
                        continue
                    sess.run(
                        "MATCH (s:Candidate {statement_id:$id}) "
                        "SET s:Verified:HumanReviewed, "
                        "    s.verification_status = 'verified_after_review', "
                        "    s.needs_review = false", id=sid)
                    flagged.discard(sid)
                    n_dispositions += 1
                    audit("verify_statements", "statement_verified_after_review",
                          statement_id=sid, check=v["check"],
                          pattern=v.get("pattern"), human_label=label,
                          source=args.reviewed)

        for v in verdicts:
            audit("verify_statements", "verdict", **v)
        for sid in sorted(flagged):
            audit("verify_statements", "statement_flagged", statement_id=sid)

        n_real = sess.run("MATCH (s:Candidate) WHERE NOT s:Synthetic "
                          "RETURN count(s)").single()[0]

    driver.close()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        for v in verdicts:
            f.write(json.dumps(v) + "\n")

    from collections import Counter
    counts = Counter((v["check"], v["verdict"]) for v in verdicts)
    resolved = sum(1 for v in verdicts
                   if (v.get("evidence") or {}).get("resolved_via_exception"))
    print(f"wrote {len(verdicts)} pair verdicts -> {args.out}")
    for (check, verdict), n in sorted(counts.items()):
        print(f"  {check:18s} {verdict:24s} {n}")
    print(f"tensions resolved via exception structures: {resolved}")
    print(f"reviewed dispositions applied from worksheet: {n_dispositions}")
    real_flagged = [s for s in flagged if not s.startswith("syn:")]
    print(f"flagged statements (held out of auto-ingest): {len(flagged)} "
          f"({len(real_flagged)} real / {n_real} real candidates)")
    for sid in sorted(flagged):
        print(f"  {sid}")


if __name__ == "__main__":
    main()
