"""Deontic predicate normalisation, shared by the extractor and the harness.

Strips a fixed, closed list of deontic scaffolding from the FRONT of a predicate
and lemmatises the single verb it exposes, leaving everything after that verb
untouched. Turns every erasure variant — `erase`, `to erase`, `shall erase`,
`shall have the obligation to erase`, `is not required to erase` — into `erase`,
while preserving genuinely distinct predicates:

  Rule 1  prefix-only — only leading scaffolding is removed and only the one
          exposed verb is lemmatised; the tail is left alone. So
          `take reasonable steps, including technical measures, to inform`
          starts with `take` (not scaffolding) and is untouched (a weaker
          standard than bare `inform`), and Art 5(1)'s six points keep distinct
          predicates (`process lawfully`, `collect for specified purposes`, …)
          instead of collapsing to their last verb.

  Rule 2  negation gated on modality — `not` is stripped only when the modality
          already carries it (DISPENSATION / PROHIBITION). A negation on an
          OBLIGATION / PERMISSION is a contradiction, not a normalisation case:
          the predicate is left untouched and `contradiction=True` is returned
          so the caller can route it to HITL rather than silently flip meaning.

`normalise_predicate(text, modality)` -> (normalised_text, contradiction_bool).
"""
import re

import nltk
from nltk.stem import WordNetLemmatizer


def _ensure_wordnet() -> None:
    try:
        nltk.data.find("corpora/wordnet")
    except LookupError:
        try:
            nltk.download("wordnet", quiet=True)
            nltk.download("omw-1.4", quiet=True)
        except Exception:
            pass  # graceful: lemmatiser then no-ops, scaffolding strip still runs


_ensure_wordnet()
_LEM = WordNetLemmatizer()

# Periphrastic obligation phrases (multi-word), matched at the front, longest
# first. Those containing `not` are negation-bearing.
_PERIPHRASTIC = [
    "shall have the obligation to",
    "has the obligation to",
    "shall have the right to",
    "is not required to",
    "is required to",
]
# Single-token scaffolding: modals, auxiliaries, the infinitive `to`.
_SCAFFOLD_TOKENS = {"shall", "may", "must", "can", "be", "been", "being", "to"}
_HAS_NOT = re.compile(r"\bnot\b")
# Leading adverbs to skip when locating the verb to lemmatise: `further
# processed` -> `further process`. A CLOSED set (not WordNet verb-detection,
# since `further` is itself a WordNet verb) so we only ever step past these
# specific modifiers to reach the intended verb — never deeper. `no longer` is
# handled as a two-word phrase.
_LEADING_ADVERBS = {"further", "duly", "promptly", "reasonably", "directly",
                    "effectively", "specifically"}


def _lemmatise_head(s: str) -> str:
    """Lemmatise the first VERB after any leading curated adverbs, in place;
    everything else is left untouched. Skipping is limited to the closed adverb
    set, so we never step past the intended verb (collapse-safe)."""
    s = s.strip()
    parts = s.split(" ")
    i = 0
    while i < len(parts):
        w = re.sub(r"[^a-z]", "", parts[i].lower())
        nxt = re.sub(r"[^a-z]", "", parts[i + 1].lower()) if i + 1 < len(parts) else ""
        if w == "no" and nxt == "longer":
            i += 2
            continue
        if w in _LEADING_ADVERBS:
            i += 1
            continue
        break
    if i < len(parts):
        m = re.match(r"([A-Za-z']+)(.*)", parts[i], re.S)
        if m:
            parts[i] = _LEM.lemmatize(m.group(1).lower(), pos="v") + m.group(2)
    return " ".join(parts).strip()


def normalise_predicate(text: str, modality: str | None = None) -> tuple[str, bool]:
    """Strip leading deontic scaffolding and lemmatise the exposed verb. Returns
    (normalised_text, contradiction). On a negation the modality doesn't carry
    (OBLIGATION/PERMISSION) the original is returned untouched with
    contradiction=True."""
    if not text or not text.strip():
        return text, False
    original = text.strip()
    s = original
    neg_ok = (modality or "").upper() in ("DISPENSATION", "PROHIBITION")

    while True:
        low = s.lower()
        stripped = False
        for ph in _PERIPHRASTIC:
            m = re.match(re.escape(ph) + r"\b\s*", low)
            if not m:
                continue
            if _HAS_NOT.search(ph) and not neg_ok:
                return original, True          # contradiction: leave + flag
            s = s[m.end():]
            stripped = True
            break
        if stripped:
            continue
        # Negation-carrying periphrases ('refrain from requesting'): the wrapper
        # double-encodes the deontic negation exactly like a leading 'not', so it
        # is stripped ONLY when the modality already carries the negation
        # (PROHIBITION/DISPENSATION). Under OBLIGATION/PERMISSION the wrapper is
        # the norm's actual content — left untouched, no flag (v3, round-2 U15:
        # PROHIBITION + 'refrain from requesting' read back as 'shall not
        # refrain from requesting', an inverted meaning).
        m = re.match(r"(refrain|abstain)\s+from\b\s*", s, re.IGNORECASE)
        if m and neg_ok:
            s = s[m.end():]
            continue
        m = re.match(r"([A-Za-z']+)\b\s*", s)
        if not m:
            break
        tok = m.group(1).lower()
        if tok in _SCAFFOLD_TOKENS:
            s = s[m.end():]
            continue
        if tok == "not":
            if neg_ok:
                s = s[m.end():]
                continue
            return original, True              # contradiction: leave + flag
        break                                  # exposed the verb

    return _lemmatise_head(s), False
