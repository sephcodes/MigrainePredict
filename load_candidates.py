#!/usr/bin/env python3
"""
load_candidates.py -- Stage 3 staging loader: content-mapped statements -> Neo4j.

Loads the canonical content-mapped run (DEV + HOLDOUT run4) into a staging graph:

  (:Statement:Candidate {statement_id, ...})   one node per non-NA statement
  (:Concept {iri, label, scheme})              every mapped slot IRI
  (:Provision {iri})                           paragraph IRIs + referenced provisions
  (s)-[:SOURCED_FROM]->(:Provision)            the statement's own paragraph
  (s)-[:REFERS_TO]->(:Provision or :Statement) from `references` ('#' = statement ref)
  (s)-[:HAS_SUBJECT|HAS_PREDICATE|HAS_OBJECT|HAS_CONDITION
        {value, method, mapping_status}]->(:Concept)
  (c)-[:BROADER]->(:Concept)                   direct vocab parents from terms.json,
                                               for subsumption-aware slot overlap

Slot provenance (mapping_status) lives on the slot edge; a statement with any
llm_suggested_* slot also gets the :LLMSuggested label, any manually_* slot the
:HumanReviewed label (FR6 provenance, ingest-with-provenance HITL decision).
Idempotent: MERGE throughout. --wipe clears the database first.

Usage:
  python load_candidates.py            # load default DEV+HOLDOUT run4 files
  python load_candidates.py --wipe     # clear the graph, then load
"""
import argparse
import datetime
import json
import os

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

TERMS_JSON = "mapping/vocab/terms.json"

DEFAULT_INPUTS = [
    ("DEV", "data/dev_5run_deontic_pred_subjmap_modmap_content/run4.content_mapped.jsonl"),
    ("HOLDOUT", "data/holdout_5run_redundant_neg_subjmap_modmap_content/run4.content_mapped.jsonl"),
]

# Top-level record keys that are structural; anything else truthy is a guard flag.
CORE_KEYS = {
    "statement", "statement_class", "statement_id", "paragraph_iri",
    "needs_review", "anchor", "classification_rationale",
    "profile_dimensions_matched",
}

SLOT_RELS = {
    "subject": "HAS_SUBJECT",
    "predicate": "HAS_PREDICATE",
    "object": "HAS_OBJECT",
    "condition": "HAS_CONDITION",
}


def load_terms(path):
    nested = json.load(open(path))
    flat = {}
    for entries in nested.values():
        flat.update(entries)
    return flat


def slot_entries(statement, slot):
    """Normalise a slot to a list of {value, method, iris, mapping_status} dicts."""
    raw = statement.get(slot)
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for it in items:
        iris = it.get("iri") or []
        if isinstance(iris, str):
            iris = [iris]
        out.append({
            "value": it.get("value", ""),
            "method": it.get("method"),
            "iris": iris,
            "mapping_status": it.get("mapping_status"),
        })
    return out


def build_row(rec, eval_set):
    """Flatten one JSONL record into node props + slot edges + refs."""
    st = rec["statement"]
    cls = rec["statement_class"]
    flags = sorted(k for k, v in rec.items() if k not in CORE_KEYS and v)

    props = {
        "statement_id": rec["statement_id"],
        "statement_class": cls,
        "eval_set": eval_set,
        "paragraph_iri": rec["paragraph_iri"],
        "source_article": st.get("source_article"),
        "confidence": st.get("confidence"),
        "needs_review": bool(rec.get("needs_review")),
        "applies_to_healthcare": st.get("applies_to_healthcare"),
        "anchor": rec.get("anchor"),
        "flags": flags,
    }

    slots = []  # (rel_type, iri, edge_props)
    if cls == "DEONTIC":
        props["modality"] = st["modality"]["value"]
        props["modality_iri"] = st["modality"]["iri"]
        props["severity"] = st.get("severity")
        if st.get("beneficiary"):
            props["beneficiary_value"] = st["beneficiary"].get("value")
            props["beneficiary_method"] = st["beneficiary"].get("method")
        for slot in ("subject", "predicate", "object", "condition"):
            entries = slot_entries(st, slot)
            props[f"{slot}_text"] = [e["value"] for e in entries]
            props[f"{slot}_status"] = [e["mapping_status"] or "" for e in entries]
            for e in entries:
                for iri in e["iris"]:
                    slots.append((SLOT_RELS[slot], iri, {
                        "value": e["value"],
                        "method": e["method"],
                        "mapping_status": e["mapping_status"] or "mapped",
                    }))
    elif cls == "DEFINITIONAL":
        props["term"] = st.get("term")
        props["definition_value"] = (st.get("definition") or {}).get("value")
        props["definition_method"] = (st.get("definition") or {}).get("method")
    elif cls == "APPLICABILITY":
        props["scope_type"] = st.get("scope_type")
        props["polarity"] = st.get("polarity")
        props["applies_to_value"] = (st.get("applies_to") or {}).get("value")
        props["applies_to_method"] = (st.get("applies_to") or {}).get("method")
        props["condition_text"] = [(st.get("condition") or {}).get("value") or ""]

    statuses = [s for _, _, ep in slots for s in [ep["mapping_status"]]]
    labels = ["Statement", "Candidate"]
    if any(s.startswith("llm_suggested") for s in statuses):
        labels.append("LLMSuggested")
    if any(s.startswith("manually") for s in statuses):
        labels.append("HumanReviewed")

    refs = st.get("references") or []
    return labels, props, slots, refs


def broader_chain(iri, terms, seen):
    """Yield (child, parent) pairs walking direct parents transitively."""
    for parent in dict.fromkeys(terms.get(iri, {}).get("parents") or []):
        if parent not in terms:
            continue  # parent outside the loaded vocabulary (e.g. full-URL module refs)
        yield iri, parent
        if parent not in seen:
            seen.add(parent)
            yield from broader_chain(parent, terms, seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", action="append", metavar="SET:PATH",
                    help="eval_set:jsonl_path (repeatable; default DEV+HOLDOUT run4)")
    ap.add_argument("--wipe", action="store_true", help="clear the database first")
    args = ap.parse_args()

    inputs = ([tuple(x.split(":", 1)) for x in args.input]
              if args.input else DEFAULT_INPUTS)
    terms = load_terms(TERMS_JSON)

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as sess:
        if args.wipe:
            sess.run("MATCH (n) DETACH DELETE n")
            audit("load_candidates", "wipe")
        for stmt in (
            "CREATE CONSTRAINT stmt_id IF NOT EXISTS FOR (s:Statement) REQUIRE s.statement_id IS UNIQUE",
            "CREATE CONSTRAINT concept_iri IF NOT EXISTS FOR (c:Concept) REQUIRE c.iri IS UNIQUE",
            "CREATE CONSTRAINT provision_iri IF NOT EXISTS FOR (p:Provision) REQUIRE p.iri IS UNIQUE",
        ):
            sess.run(stmt)

        n_stmt, n_edge, used_concepts = 0, 0, set()
        for eval_set, path in inputs:
            for line in open(path):
                rec = json.loads(line)
                if rec["statement_class"] == "NOT_APPLICABLE":
                    continue
                labels, props, slots, refs = build_row(rec, eval_set)
                sess.run(
                    f"MERGE (s:Statement {{statement_id: $id}}) "
                    f"SET s:{':'.join(labels[1:])}, s += $props",
                    id=props["statement_id"], props=props)
                sess.run(
                    "MATCH (s:Statement {statement_id: $id}) "
                    "MERGE (p:Provision {iri: $prov}) "
                    "MERGE (s)-[:SOURCED_FROM]->(p)",
                    id=props["statement_id"], prov=props["paragraph_iri"])
                for rel, iri, eprops in slots:
                    meta = terms.get(iri, {})
                    sess.run(
                        f"MATCH (s:Statement {{statement_id: $id}}) "
                        f"MERGE (c:Concept {{iri: $iri}}) "
                        f"SET c.label = $label, c.scheme = $scheme "
                        f"MERGE (s)-[r:{rel} {{value: $value}}]->(c) "
                        f"SET r.method = $method, r.mapping_status = $status",
                        id=props["statement_id"], iri=iri,
                        label=meta.get("label"), scheme=meta.get("scheme"),
                        value=eprops["value"], method=eprops["method"],
                        status=eprops["mapping_status"])
                    used_concepts.add(iri)
                    n_edge += 1
                for ref in refs:
                    if "#" in ref:
                        sess.run(
                            "MATCH (s:Statement {statement_id: $id}) "
                            "MERGE (t:Statement {statement_id: $ref}) "
                            "MERGE (s)-[:REFERS_TO {kind: 'statement'}]->(t)",
                            id=props["statement_id"], ref=ref)
                    else:
                        sess.run(
                            "MATCH (s:Statement {statement_id: $id}) "
                            "MERGE (p:Provision {iri: $ref}) "
                            "MERGE (s)-[:REFERS_TO {kind: 'provision'}]->(p)",
                            id=props["statement_id"], ref=ref)
                audit("load_candidates", "statement_loaded",
                      statement_id=props["statement_id"], eval_set=eval_set,
                      statement_class=props["statement_class"],
                      labels=labels[2:], slot_edges=len(slots),
                      confidence=props.get("confidence"))
                n_stmt += 1

        # BROADER hierarchy for every used concept (transitive through terms.json)
        pairs, seen = set(), set()
        for iri in used_concepts:
            pairs.update(broader_chain(iri, terms, seen))
        for child, parent in sorted(pairs):
            meta = terms.get(parent, {})
            sess.run(
                "MERGE (c:Concept {iri: $child}) "
                "MERGE (p:Concept {iri: $parent}) "
                "SET p.label = coalesce(p.label, $label), "
                "    p.scheme = coalesce(p.scheme, $scheme) "
                "MERGE (c)-[:BROADER]->(p)",
                child=child, parent=parent,
                label=meta.get("label"), scheme=meta.get("scheme"))

        counts = sess.run(
            "MATCH (s:Statement) WITH count(s) AS st "
            "MATCH (c:Concept) WITH st, count(c) AS co "
            "MATCH (p:Provision) WITH st, co, count(p) AS pr "
            "OPTIONAL MATCH ()-[b:BROADER]->() "
            "RETURN st, co, pr, count(b) AS br").single()
        print(f"loaded {n_stmt} statements, {n_edge} slot edges | graph: "
              f"{counts['st']} :Statement, {counts['co']} :Concept, "
              f"{counts['pr']} :Provision, {counts['br']} BROADER edges")
    driver.close()


if __name__ == "__main__":
    main()
