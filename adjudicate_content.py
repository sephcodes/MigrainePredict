"""
Tier-3 LLM adjudicator for the content-mapping review tail.

The deterministic matcher (build_content_candidates.py) auto-maps objects on
strong lexical hits and auto-literals predicate/condition residue, and leaves the
genuinely-undecided rows as status='review' with a candidate list. This script
resolves those 'review' rows with one Gemini call each, mirroring the extractor's
setup (LangChain ChatPromptTemplate + Pydantic with_structured_output + Gemini
2.5 Flash + _retry_invoke).

Design (all agreed):
  - PROPOSE-ONLY: writes status='llm_suggested_mapped' / 'llm_suggested_literal' /
    'llm_suggested_flag' (never plain mapped/literal/flag, never manually_*), plus
    llm_confidence and llm_rationale for your audit pass.
  - CANDIDATE-CONSTRAINED: a 'mapped' decision may only use IRIs from the row's own
    _candidates. An out-of-candidate IRI triggers one corrective retry, then the row
    is escalated (status='escalated') rather than trusting an invented IRI.
  - ESCALATION: confidence < --threshold -> status='escalated' (your queue).
  - review-only: rows with any other status (incl. manually_* and llm_suggested_*)
    are left untouched.

Accepting a suggestion in your audit = editing 'llm_suggested_mapped' -> 'manually_mapped'
(promotion locks it; build_content_candidates.preserve_manual then preserves it).

Usage:
    python adjudicate_content.py                          # Gemini, mapping/content_map.json
    python adjudicate_content.py --backend mistral        # Ollama fallback
    python adjudicate_content.py --limit 5                # smoke-test 5 review rows
    python adjudicate_content.py --threshold 0.6          # escalation cutoff
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from tqdm import tqdm

HERE = os.path.dirname(__file__)
CONTENT_MAP = os.path.join(HERE, "mapping", "content_map.json")
TERMS = os.path.join(HERE, "mapping", "vocab", "terms.json")
ROUTING = os.path.join(HERE, "mapping", "slot_routing.json")


def load_routed_vocab():
    """Mirror build_content_candidates.load_targets: resolve slot x regulation ->
    {iri: (label, scheme_or_root)} over terms.json + slot_routing.json, so the
    adjudicator can choose from (and is validated against) the FULL routed
    vocabulary for a slot, not just the matcher's top-k candidates."""
    terms = json.load(open(TERMS))
    routing = json.load(open(ROUTING))
    exclude = set(routing.get("_exclude_abstract", []))
    out = {}
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            picked = {}
            for sel in routing[slot][reg]:
                voc = terms[sel["vocab"]]
                if sel["by"] == "all":
                    picked.update({c: (r["label"], r.get("scheme") or r.get("root")) for c, r in voc.items()})
                elif sel["by"] == "scheme":
                    want = set(sel["values"])
                    picked.update({c: (r["label"], r["scheme"]) for c, r in voc.items() if r["scheme"] in want})
                elif sel["by"] == "root":
                    want = set(sel["values"])
                    picked.update({c: (r["label"], r.get("root")) for c, r in voc.items() if r.get("root") in want})
            for c in exclude:
                picked.pop(c, None)
            out[(slot, reg)] = picked
    return out


def format_vocab(vocab: dict) -> str:
    """Group a {iri: (label, scheme)} routed vocab by scheme for the prompt."""
    from collections import defaultdict
    by_scheme = defaultdict(list)
    for iri, (label, scheme) in vocab.items():
        by_scheme[scheme or "(ungrouped)"].append(f"    {iri} | {label}")
    lines = []
    for scheme in sorted(by_scheme):
        lines.append(f"  [{scheme}]")
        lines.extend(sorted(by_scheme[scheme]))
    return "\n".join(lines)

GEMINI_MODEL = "gemini-2.5-flash"
MISTRAL_MODEL = "mistral-nemo"
REQUEST_TIMEOUT_SECONDS = 60
MAX_CONCURRENT = 20
ESCALATION_THRESHOLD = 0.7   # confidence < τ -> status='escalated' (mirrors extractor HITL_THRESHOLD)

_TRANSIENT_ERROR_MARKERS = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
    "timeout", "Timeout", "TIMEOUT", "deadline", "Deadline", "DEADLINE",
    "Connection reset", "ECONNRESET",
)
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 2.0
RETRY_BACKOFF_FACTOR = 3.0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class AdjudicationDecision(BaseModel):
    disposition: Literal["mapped", "literal", "flag"] = Field(
        description=(
            "'mapped' = the fragment names one or more ontology concepts that ARE "
            "present in the candidate list. 'literal' = the fragment is a "
            "qualifier / manner / medium / temporal / procedural / cross-reference "
            "clause that is not an ontology concept. 'flag' = the fragment names a "
            "genuine regulatory concept that has NO matching candidate (a real gap)."
        )
    )
    chosen_iris: list[str] = Field(
        default_factory=list,
        description=(
            "For disposition='mapped', the IRI(s) of the chosen concept(s), taken "
            "ONLY from the provided candidate list — one per distinct concept the "
            "fragment names (multi-concept fragments map to several). Empty list "
            "for 'literal' and 'flag'."
        ),
    )
    confidence: float = Field(
        description="Your certainty in this disposition, from 0.0 to 1.0."
    )
    rationale: str = Field(
        description="One short sentence justifying the disposition."
    )


# ---------------------------------------------------------------------------
# Prompts (CoI layout, system/user separation, like the extractor)
# ---------------------------------------------------------------------------


ADJUDICATOR_SYSTEM_PROMPT = """\
You adjudicate how a single fragment of EU regulatory text (GDPR or the EU AI Act)
maps to a controlled compliance vocabulary (DPV / AIRO / VAIR). You are given the
fragment, the slot it fills (predicate = an action, object = what a duty acts on,
condition = a precondition/trigger), its source article, and a CANDIDATE LIST of
vocabulary concepts the deterministic matcher retrieved for it. Choose exactly one
disposition and, if 'mapped', the candidate IRI(s).

HARD RULES — follow them exactly:

1. chosen_iris MUST be drawn ONLY from the ROUTED VOCABULARY given below for this
   slot (the full list of concepts valid for this slot and regulation). The
   "matcher hits" are the deterministic matcher's top candidates — a STRONG signal,
   usually your answer — but the full vocabulary is provided so you can pick the
   right concept when the matcher missed it (e.g. the fragment's surface form
   differs from the concept's label). NEVER output an IRI that is not in the routed
   vocabulary. If no concept in the vocabulary fits, use 'flag' (a genuine
   regulatory concept the vocabulary lacks) or 'literal' (not a concept at all).

2. Candidate 'method' matters. method=lexical / exact / synonym / alias means the
   fragment's own words matched the concept — trustworthy. method=embed is a mere
   similarity hint and is NOT evidence: a high embed score (0.6-0.9) is returned
   for the NEAREST concept even when the right answer is none. Do NOT map a
   fragment just because an embed candidate scored highly. Rely on lexical-family
   hits; treat embed as a weak suggestion to sanity-check, not to trust.

3. Legal-basis concepts (labels like a legal ground under Art 6 or Art 9) denote a
   SPECIFIC lawful basis for processing. Do NOT assign a legal-basis IRI to a
   descriptive or procedural clause that merely shares a word with it. Example: a
   clause about processing 'on behalf of a controller' is NOT the 'official
   authority of the controller' legal basis.

4. These are 'literal' (not concepts): manner ('in a concise, transparent form'),
   medium ('in writing, by electronic means'), vague/most temporal qualifiers
   ('without undue delay'), and cross-references ('the provisions of paragraphs 1
   and 2', 'referred to in Article X'). A specific numeric threshold with no
   candidate concept (e.g. a 72-hour deadline, a 250-employee exemption) is a
   'flag', not 'literal' — it is a real regulatory parameter the vocabulary cannot
   represent.

5. GENERIC RIDE-ALONGS: a broad one-word concept (e.g. Law, Contract, Scope,
   Standard, Service, Notice) that matches only a single incidental word inside a
   long, multi-clause fragment is almost always NOT what the fragment is about — do
   NOT map to it. Map to a concept only when the fragment genuinely names or is
   centrally about it. If both a specific and a generic concept seem to apply,
   choose the specific one; if only a generic rides along on a long clause, the
   disposition is 'literal' (or 'flag'), not a map to the generic. (Some of these
   words ARE the right concept when the fragment is truly about them — e.g. a clause
   whose point is a contractual legal basis maps to Contract — so judge by what the
   fragment is ABOUT, not by mere word presence.)

6. A fragment may name MORE THAN ONE concept — return every candidate IRI that the
   fragment genuinely names (e.g. a clause naming both a data category and a
   measure maps to both).

7. Set confidence honestly. Use it low when the candidates are weak/embed-only, the
   fragment is ambiguous, or you are guessing — those rows will be escalated to a
   human.
"""

ADJUDICATOR_USER_PROMPT = """\
Slot: {slot}   Regulation: {regulation}   Source article: {source_article}

Fragment:
{value}

Matcher hits (the deterministic matcher's top candidates — strong signal, iri | label | method | score):
{candidates}

Routed vocabulary for {slot}.{regulation} — you MUST choose chosen_iris from THIS list
(grouped by scheme; the matcher hits above are a subset of it):
{vocabulary}
{correction}
Adjudicate this fragment."""


def _format_candidates(cands: list[dict]) -> str:
    if not cands:
        return "  (none — the matcher found no candidate; choose 'literal' or 'flag')"
    lines = []
    for c in cands:
        lines.append(
            f"  {c.get('iri')} | {c.get('label')} | {c.get('method')} | {c.get('score')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM setup (mirrors extract_min.py._llm / build_chains / _retry_invoke)
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    env = Path(HERE) / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _llm(backend: str):
    if backend == "mistral":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=MISTRAL_MODEL, temperature=0)
    if backend == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        _load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) not set; add to .env or export"
            )
        return ChatGoogleGenerativeAI(
            model=GEMINI_MODEL,
            temperature=0,
            google_api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    raise ValueError(f"unknown backend {backend!r}; choose 'mistral' or 'gemini'")


def build_chain(backend: str):
    llm = _llm(backend)
    return ChatPromptTemplate.from_messages(
        [("system", ADJUDICATOR_SYSTEM_PROMPT), ("user", ADJUDICATOR_USER_PROMPT)]
    ) | llm.with_structured_output(AdjudicationDecision)


def _retry_invoke(chain, payload: dict, *, label: str):
    """Invoke with exponential backoff on transient errors (mirrors extractor)."""
    last_exc: Optional[Exception] = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            return chain.invoke(payload)
        except Exception as e:
            last_exc = e
            msg = str(e)
            is_transient = any(m in msg for m in _TRANSIENT_ERROR_MARKERS)
            if not is_transient or attempt == RETRY_MAX_ATTEMPTS - 1:
                raise
            delay = RETRY_BASE_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR ** attempt)
            print(f"  retry {label} (attempt {attempt+1}/{RETRY_MAX_ATTEMPTS}) "
                  f"after {delay:.0f}s — transient: {msg[:120]}")
            time.sleep(delay)
    raise last_exc  # pragma: no cover


# ---------------------------------------------------------------------------
# Adjudication of one review row
# ---------------------------------------------------------------------------


def _payload(row: dict, slot: str, reg: str, vocab_text: str, correction: str = "") -> dict:
    return {
        "slot": slot,
        "regulation": reg,
        "source_article": row.get("source_article", ""),
        "value": row["value"],
        "candidates": _format_candidates(row.get("_candidates", [])),
        "vocabulary": vocab_text,
        "correction": ("\n" + correction + "\n") if correction else "",
    }


def _validate(decision: AdjudicationDecision, allowed: set[str]) -> Optional[str]:
    """Return None if the decision is well-formed against the routed vocabulary, else
    a correction string describing exactly what was wrong (for one retry)."""
    if decision.disposition == "mapped":
        if not decision.chosen_iris:
            return "You chose 'mapped' but returned no IRIs. Pick vocabulary IRI(s), or use 'literal'/'flag'."
        bad = [i for i in decision.chosen_iris if i not in allowed]
        if bad:
            return (
                f"You returned IRI(s) not in the routed vocabulary: {bad}. "
                f"Choose ONLY from the routed vocabulary list, or use 'flag' if no "
                f"concept fits."
            )
    return None


def adjudicate_row(chain, row: dict, slot: str, reg: str, threshold: float,
                   allowed: set[str], vocab_text: str) -> dict:
    """Return the fields to write onto the row (does not mutate in place).
    `allowed` is the full routed-vocabulary IRI set for (slot, reg)."""
    label = f"{slot}.{reg} {row['value'][:40]!r}"

    decision = _retry_invoke(chain, _payload(row, slot, reg, vocab_text), label=label)
    problem = _validate(decision, allowed)
    if problem is not None:  # one corrective retry naming the violation
        decision = _retry_invoke(chain, _payload(row, slot, reg, vocab_text, correction="CORRECTION: " + problem),
                                 label=label + " [retry]")
        if _validate(decision, allowed) is not None:  # still bad -> escalate, don't trust it
            return {"status": "escalated", "iri": [],
                    "llm_confidence": round(float(decision.confidence), 3),
                    "llm_rationale": "auto-escalated: model returned out-of-vocabulary IRI twice"}

    conf = round(float(decision.confidence), 3)
    if conf < threshold:
        return {"status": "escalated", "iri": [],
                "llm_confidence": conf, "llm_rationale": decision.rationale}

    iris = decision.chosen_iris if decision.disposition == "mapped" else []
    return {"status": f"llm_suggested_{decision.disposition}", "iri": iris,
            "llm_confidence": conf, "llm_rationale": decision.rationale}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="gemini", choices=["gemini", "mistral"])
    ap.add_argument("--limit", type=int, default=None, help="adjudicate only the first N review rows (smoke test)")
    ap.add_argument("--threshold", type=float, default=ESCALATION_THRESHOLD,
                    help="confidence below this -> status='escalated'")
    ap.add_argument("--path", default=CONTENT_MAP, help="content_map.json to adjudicate in place")
    args = ap.parse_args(argv)

    cm = json.load(open(args.path))
    # collect (slot, reg, row) for every review row, in stable order
    jobs = []
    for slot in ("predicate", "object", "condition"):
        for reg in ("gdpr", "aiact"):
            for row in cm.get(slot, {}).get(reg, []):
                if row.get("status") == "review":
                    jobs.append((slot, reg, row))
    if args.limit:
        jobs = jobs[:args.limit]
    if not jobs:
        print("no review rows to adjudicate.")
        return
    print(f"adjudicating {len(jobs)} review rows via {args.backend} (escalate < {args.threshold})")

    routed = load_routed_vocab()                                  # {(slot,reg): {iri:(label,scheme)}}
    # object and condition already share a merged content vocabulary in slot_routing.json
    # (grammatical slot != concept kind), so read routing directly — no special-casing.
    allowed_by = {k: set(v.keys()) for k, v in routed.items()}
    vocab_text_by = {k: format_vocab(v) for k, v in routed.items()}

    chain = build_chain(args.backend)
    results: dict[int, dict] = {}

    def work(i, slot, reg, row):
        return i, adjudicate_row(chain, row, slot, reg, args.threshold,
                                 allowed_by[(slot, reg)], vocab_text_by[(slot, reg)])

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
        futs = [ex.submit(work, i, s, r, row) for i, (s, r, row) in enumerate(jobs)]
        for fut in tqdm(as_completed(futs), total=len(futs), desc="adjudicate"):
            try:
                i, patch = fut.result()
            except Exception as e:
                # hard failure after retries -> escalate that row, keep going
                i = next(k for k, f in enumerate(futs) if f is fut)
                patch = {"status": "escalated", "iri": [], "llm_confidence": 0.0,
                         "llm_rationale": f"adjudicator error: {str(e)[:120]}"}
            results[i] = patch

    from collections import Counter
    tally = Counter()
    for i, (slot, reg, row) in enumerate(jobs):
        patch = results[i]
        row.update(patch)
        tally[patch["status"]] += 1

    json.dump(cm, open(args.path, "w"), indent=2, ensure_ascii=False)
    print(f"\nwrote {args.path}")
    print("disposition of adjudicated rows:", dict(tally))


if __name__ == "__main__":
    main()