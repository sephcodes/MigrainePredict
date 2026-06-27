"""Shared subject->role-IRI resolution for the mapping (stage 2) step.

Deterministic lexicon lookup, no LLM. The lexicon lives in
mapping/subject_lexicon.json (alias -> canonical role IRI, per regulation side).
Surface forms are normalised with the SAME normaliser the grading harness uses
(compare_to_gold.norm_text: NFKC + lowercase + collapse whitespace + strip a
leading determiner) so determiner/case/whitespace variants collapse without
needing an alias row for each.

Resolution is regulation-aware: a form whose role belongs to a different
regulation than the record's source_article prefix is reported as a mismatch
(cross-contamination), never silently mapped.
"""
import json
import os

from compare_to_gold import norm_text

LEXICON_PATH = os.path.join(os.path.dirname(__file__), "mapping", "subject_lexicon.json")


def load_lexicon(path=LEXICON_PATH):
    """Load the lexicon and build a normalised alias index.

    Returns (lexicon_dict, alias_index) where alias_index maps
    norm_text(alias) -> role dict ({iri, vocab, regulation, aliases}).
    """
    with open(path) as fh:
        lex = json.load(fh)
    index = {}
    for role in lex.get("roles", []):
        for alias in role.get("aliases", []):
            key = norm_text(alias)
            if key in index and index[key]["iri"] != role["iri"]:
                raise ValueError(
                    f"ambiguous alias {alias!r} -> {index[key]['iri']} and {role['iri']}"
                )
            index[key] = role
    return lex, index


def regulation_of(source_article):
    """Regulation side from an IRI prefix, e.g. 'gdpr:art_6/...' -> 'gdpr'."""
    if not source_article:
        return None
    return source_article.split(":", 1)[0]


def resolve(value, regulation, index):
    """Resolve a subject surface form to a role for the given regulation side.

    Returns (iri, status):
      - (iri, "mapped")     lexicon hit whose regulation matches
      - (None, "unmapped")  no lexicon hit for the normalised form
      - (None, "mismatch")  lexicon hit, but for the wrong regulation side
    """
    role = index.get(norm_text(value or ""))
    if role is None:
        return None, "unmapped"
    if regulation is not None and role.get("regulation") != regulation:
        return None, "mismatch"
    return role["iri"], "mapped"
