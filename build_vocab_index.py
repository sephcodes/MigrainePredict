"""Parse the downloaded DP (core + eu-gdpr, sector-health, justifications
extensions) / PD / AIRO / VAIR turtle files into a complete,
organised term index (mapping/vocab/terms.json), so the predicate/object/
condition coverage pass matches against the FULL published vocabularies.

Uses rdflib for correct turtle parsing (earlier regex splitting broke on
multi-line comment strings, dropped separately-declared skos:prefLabels, and
mis-typed object properties whose blank-node domains mention owl:Class).

Concepts only: subjects typed rdfs:Class / owl:Class / skos:Concept, excluding
object/datatype/annotation properties. Per concept we capture label, scheme,
types, parents, and a `root` (topmost ancestor via subClassOf/broader).

Grouping:
  - DPV / PD: by skos:inScheme.
  - AIRO / VAIR: by `root`, since VAIR's inScheme is a single vair: scheme for
    the whole vocabulary; the purpose/domain/risk-source/control partition is
    carried by the subClassOf root (e.g. vair:LawEnforcement -> airo:Domain,
    vair:Intervention -> airo:RiskControl).

The report at the end prints the agreed slot x regulation routing targets in slout_routing.json

Usage: python build_vocab_index.py   (reads mapping/vocab/*.ttl)
"""
import json
import os
import re
from collections import Counter

from rdflib import Graph, RDF, RDFS, OWL
from rdflib.namespace import SKOS
from rdflib.term import URIRef

VOCAB_DIR = os.path.join(os.path.dirname(__file__), "mapping", "vocab")
FILES = ["dpv", "pd", "airo", "vair", "eu-gdpr", "sector-health", "justifications"]

CLASS_TYPES = {RDFS.Class, OWL.Class, SKOS.Concept}
PROP_TYPES = {OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty, RDF.Property}

# longest namespace first so pd/ matches before dpv/
NS = [
    ("eu-gdpr", "https://w3id.org/dpv/legal/eu/gdpr#"),
    ("sector-health", "https://w3id.org/dpv/sector/health#"),
    ("justifications", "https://w3id.org/dpv/justifications#"),
    ("pd", "https://w3id.org/dpv/pd#"),
    ("dpv", "https://w3id.org/dpv#"),
    ("airo", "https://w3id.org/airo#"),
    ("vair", "http://w3id.org/vair#"),
    ("vair", "https://w3id.org/vair#"),
]


def curie(uri):
    u = str(uri)
    for p, ns in NS:
        if u.startswith(ns):
            return f"{p}:{u[len(ns):]}"
    return u


def delabel(curie_str):
    """Human-readable label derived from a CamelCase localname, for the handful
    of (mostly VAIR) terms that ship without a prefLabel/rdfs:label."""
    ln = curie_str.split(":", 1)[-1]
    ln = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", ln)   # split camelCase
    ln = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", ln)  # split ACRONYMWord
    return ln.replace("_", " ").strip().lower()


def parse_graph(path):
    g = Graph()
    g.parse(path, format="turtle")
    recs = {}
    for s in set(g.subjects(RDF.type, None)):
        if not isinstance(s, URIRef):
            continue
        types = set(g.objects(s, RDF.type))
        if types & PROP_TYPES:
            continue
        if not (types & CLASS_TYPES):
            continue
        label = g.value(s, SKOS.prefLabel) or g.value(s, RDFS.label)
        scheme = g.value(s, SKOS.inScheme)
        parents = [curie(o) for rel in (RDFS.subClassOf, SKOS.broader)
                   for o in g.objects(s, rel) if isinstance(o, URIRef)]
        recs[curie(s)] = {
            "label": str(label) if label is not None else None,
            "scheme": curie(scheme) if scheme is not None else None,
            "types": sorted(curie(t) for t in types),
            "parents": parents,
        }
    return recs


def assign_roots(recs):
    """root = topmost ancestor reachable via parents within `recs`."""
    def climb(c, seen):
        if c in seen:
            return c
        seen.add(c)
        for p in recs.get(c, {}).get("parents", []):
            if p in recs:
                return climb(p, seen)
        return c
    for c in recs:
        recs[c]["root"] = climb(c, set())


def main():
    index = {f: parse_graph(os.path.join(VOCAB_DIR, f"{f}.ttl")) for f in FILES}

    # AIRO/VAIR: resolve roots over the COMBINED set so vair->airo chains climb
    combined = {**index["airo"], **index["vair"]}
    assign_roots(combined)
    for f in ("airo", "vair"):
        for c in index[f]:
            index[f][c]["root"] = combined[c]["root"]

    # restrict each file to its OWN namespace (drops cross-ns import stubs and
    # owl-tooling-namespace artifacts), and add a derived label where missing
    own = {"dpv": "dpv:", "pd": "pd:", "airo": "airo:", "vair": "vair:", "eu-gdpr": "eu-gdpr:", "sector-health": "sector-health:", "justifications": "justifications:"}
    for f in FILES:
        kept = {}
        for c, r in index[f].items():
            if not c.startswith(own[f]):
                continue
            r["label_derived"] = not r["label"]
            if not r["label"]:
                r["label"] = delabel(c)
            kept[c] = r
        index[f] = kept

    # ---- routing targets (slot x regulation -> vocabulary), with live counts ----
    dsch = Counter(r["scheme"] for r in index["dpv"].values() if r["scheme"])
    vroot = Counter(r["root"] for r in index["vair"].values())
    pd_n, airo_n = len(index["pd"]), len(index["airo"])

    def d(s):
        return dsch.get(f"dpv:{s}", 0)

    def vr(s):
        return vroot.get(f"airo:{s}", 0)

    print("=== ROUTING TARGETS (term counts) ===")
    print(f"predicate.gdpr  -> dpv:processing-classes ({d('processing-classes')})")
    print("predicate.aiact -> (no verb taxonomy in AIRO/VAIR; structural gap, ~0)")
    print(f"object.gdpr     -> personal-data ({d('personal-data-classes')}) + PD ({pd_n}) "
          f"+ TOM ({d('TOM-classes')}) + technical-measures ({d('technical-measures-classes')}) "
          f"+ organisational-measures ({d('organisational-measures-classes')}) "
          f"+ entities-authority ({d('entities-authority-classes')}) + risk ({d('risk-classes')})")
    print(f"object.aiact    -> AIRO classes ({airo_n}) + VAIR roots "
          f"AISystem ({vr('AISystem')}) AICapability ({vr('AICapability')}) "
          f"AIComponent ({vr('AIComponent')}) RiskControl ({vr('RiskControl')})")
    print(f"condition.gdpr  -> legal-basis ({d('legal-basis-classes')}) + purposes ({d('purposes-classes')})")
    print(f"condition.aiact -> VAIR roots Purpose ({vr('Purpose')}) Domain ({vr('Domain')}) "
          f"RiskSource ({vr('RiskSource')}) RiskControl ({vr('RiskControl')}) "
          f"HumanInvolvement ({vr('HumanInvolvement')}) + AIRO ({airo_n})")

    # label coverage sanity
    print("\n=== label coverage ===")
    for f in FILES:
        tot = len(index[f])
        nl = sum(1 for r in index[f].values() if not r["label"])
        print(f"  {f:5s} {tot} concepts, {nl} missing label")

    out = os.path.join(VOCAB_DIR, "terms.json")
    with open(out, "w") as fh:
        json.dump(index, fh, indent=2, ensure_ascii=False)
    print(f"\nwrote {out}  (" + ", ".join(f"{k}={len(v)}" for k, v in index.items()) + ")")


if __name__ == "__main__":
    main()
