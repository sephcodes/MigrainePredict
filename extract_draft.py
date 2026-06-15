"""
Two-stage LLM extraction of deontic / definitional / applicability statements
from post-screened regulatory paragraphs (GDPR, EU AI Act).

  Stage 1 (classifier): for each paragraph, return a list of statement
  candidates, each tagged with one of {DEONTIC, DEFINITIONAL, APPLICABILITY,
  NOT_APPLICABLE}. A paragraph may carry multiple statements of mixed classes
  (e.g. GDPR Art 5(1)'s six principles); if it carries nothing extractable,
  a single NOT_APPLICABLE candidate is emitted (and recorded for audit).

  Stage 2 (structured extractor): one call per non-NA candidate, with the
  Pydantic schema specific to the candidate's class. Each candidate carries an
  `anchor` (short snippet/paraphrase) so the extractor knows which statement in
  the paragraph to fill in.

Prompts follow Galli et al.'s Chain-of-Instructions (CoI) layout with
system/user separation. The ExtractedValue.method taxonomy is tightened per
Chung et al.: BACKGROUND_KNOWLEDGE is removed, so any element not supported by
the paragraph + parent + siblings + cited-provision bundle must be marked NONE.

Input:  data/{source}.postscreened.jsonl
Output: data/{source}.extracted.jsonl   (smoke variant: *.extracted.smoke.jsonl)

Usage:
    python extract.py                                  # both files, Gemini
    python extract.py --backend mistral                # Ollama mistral-nemo
    python extract.py --limit 5                        # smoke-test 5 paragraphs/file
    python extract.py --limit 5 data/gdpr.postscreened.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from tqdm import tqdm

DEFAULT_PATHS = [
    Path("data/gdpr.postscreened.jsonl"),
    Path("data/aiact.postscreened.jsonl"),
]

MISTRAL_MODEL = "mistral-nemo"
GEMINI_MODEL = "gemini-2.5-flash"

# Each paragraph fires 1 stage-1 call + N stage-2 calls (one per identified
# statement). Average ~2-3 calls/paragraph, so a smaller per-batch count keeps
# us under Gemini 2.5 Flash's 1,000 req/min ceiling with the same 60s pause.
BATCH_SIZE = 200
BATCH_WAIT_SECONDS = 60
MAX_CONCURRENT = 20
REQUEST_TIMEOUT_SECONDS = 60
# Per-paragraph work is multi-call, so the batch-level deadline is roomier than
# screen.py's. Stuck paragraphs flush as NA stubs with `extractor_error` set.
BATCH_DEADLINE_SECONDS = 600

HITL_THRESHOLD = 0.7  # confidence < τ → needs_review = True


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ExtractionMethod(str, Enum):
    """How an ExtractedValue's value was obtained.

    BACKGROUND_KNOWLEDGE (in Galli's original taxonomy) is intentionally
    absent: per Chung et al., any element not supported by the bundled
    context (paragraph + parent + siblings + cited provisions) must be NONE."""

    STATED = "STATED"
    CONTEXT = "CONTEXT"
    CITATION = "CITATION"
    NONE = "NONE"


class ExtractedValue(BaseModel):
    value: Optional[str] = Field(
        description=(
            "The extracted text for this element. Set to null ONLY when method "
            "is NONE. When method is STATED/CONTEXT/CITATION, value must be a "
            "non-empty string."
        )
    )
    method: ExtractionMethod = Field(
        description=(
            "How the value was obtained. STATED = present in the paragraph "
            "text. CONTEXT = drawn from the parent chain or a sibling paragraph "
            "in the same unit. CITATION = drawn from a cross-referenced "
            "provision whose text is bundled below. NONE = the element is "
            "absent or unsupported by the bundled context. Never infer from "
            "general knowledge — if the bundle does not support it, use NONE."
        )
    )


class Modality(str, Enum):
    OBLIGATION = "OBLIGATION"
    PERMISSION = "PERMISSION"
    PROHIBITION = "PROHIBITION"
    DISPENSATION = "DISPENSATION"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ScopeAxis(str, Enum):
    MATERIAL = "MATERIAL"
    TERRITORIAL = "TERRITORIAL"
    PERSONAL = "PERSONAL"
    TEMPORAL = "TEMPORAL"


class Polarity(str, Enum):
    INCLUDES = "INCLUDES"
    EXCLUDES = "EXCLUDES"


class StatementClass(str, Enum):
    DEONTIC = "DEONTIC"
    DEFINITIONAL = "DEFINITIONAL"
    APPLICABILITY = "APPLICABILITY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DeonticStatement(BaseModel):
    modality: Modality = Field(
        description="OBLIGATION, PERMISSION, PROHIBITION, or DISPENSATION (release from an obligation)."
    )
    subject: list[ExtractedValue] = Field(
        description=(
            "Addressee(s) — the actor(s) on whom the modality acts (e.g., "
            "'the controller', 'providers', 'Member States'). Multi-valued: "
            "emit one ExtractedValue per distinct addressee."
        )
    )
    predicate: list[ExtractedValue] = Field(
        description=(
            "Deontic action verb phrase(s) — e.g., 'shall ensure', 'shall not "
            "process', 'may transfer'. Predicate may be passive when the "
            "statement is an obligation of being ('shall be secure'). "
            "Multi-valued: emit one ExtractedValue per distinct verb phrase."
        )
    )
    object: list[ExtractedValue] = Field(
        description=(
            "Target(s) the predicate acts on. Must differ from subject. "
            "Multi-valued."
        )
    )
    condition: Optional[ExtractedValue] = Field(
        default=None,
        description=(
            "Preconditions or qualifying circumstances that trigger or "
            "restrict the statement (collapses Galli's Specifications + "
            "PreConditions). Null if absent."
        ),
    )
    beneficiary: Optional[ExtractedValue] = Field(
        default=None,
        description=(
            "The party that benefits from the obligation (e.g., 'the data "
            "subject' for a right-to-access obligation). Null if absent."
        ),
    )
    source_article: str = Field(
        description="The canonical paragraph IRI given in the prompt — copy it verbatim."
    )
    references: Optional[list[str]] = Field(
        default=None,
        description=(
            "Subset of the resolved cross-reference IRIs listed in the prompt "
            "that this specific statement cites. Use null (or []) if none apply."
        ),
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="Self-rated extraction confidence in [0,1].",
    )
    applies_to_healthcare: bool = Field(
        description=(
            "True iff the statement is operationally relevant to MigrainePredict "
            "(biometric/health data; high-risk AI under Annex III or "
            "safety-component-of-medical-device; obligations on providers / "
            "deployers of high-risk AI; data-subject rights to health-related "
            "processing)."
        )
    )
    severity: Severity = Field(
        description=(
            "high = fines-tier (GDPR Art 83(5)/(6) anchor articles, AI Act Art "
            "99(3)/(4)) or anything conditioning lawfulness of processing / "
            "placement on the market; medium = procedural / documentation / "
            "notification obligations whose breach is fineable but not "
            "market-blocking; low = housekeeping (record-keeping form, "
            "retention metadata, staff designations)."
        )
    )


class DefinitionalStatement(BaseModel):
    term: str = Field(
        description=(
            "The term being defined, in canonical form (e.g., 'personal data', "
            "'biometric data', 'high-risk AI system')."
        )
    )
    definition: ExtractedValue = Field(
        description="The definitional text + extraction method."
    )
    source_article: str = Field(
        description="The canonical paragraph IRI given in the prompt — copy it verbatim."
    )
    references: Optional[list[str]] = Field(
        default=None,
        description=(
            "Subset of the resolved cross-reference IRIs listed in the prompt "
            "that this definition cites. Use null (or []) if none apply."
        ),
    )
    confidence: float = Field(ge=0, le=1, description="Self-rated extraction confidence in [0,1].")
    applies_to_healthcare: bool = Field(
        description=(
            "True iff the defined term is operationally relevant to "
            "MigrainePredict (biometric/health data; high-risk AI; safety "
            "component of a medical device; etc.)."
        )
    )


class ApplicabilityStatement(BaseModel):
    scope_type: ScopeAxis = Field(
        description=(
            "MATERIAL (subject-matter scope), TERRITORIAL (geographic), "
            "PERSONAL (who the regulation binds), TEMPORAL (when it applies)."
        )
    )
    applies_to: ExtractedValue = Field(
        description="The entity, activity, or class the scope qualifies."
    )
    condition: ExtractedValue = Field(
        description="The condition that triggers applicability or exclusion."
    )
    polarity: Polarity = Field(
        description="INCLUDES (brings within scope) or EXCLUDES (places outside scope)."
    )
    source_article: str = Field(
        description="The canonical paragraph IRI given in the prompt — copy it verbatim."
    )
    references: Optional[list[str]] = Field(
        default=None,
        description=(
            "Subset of the resolved cross-reference IRIs listed in the prompt "
            "that this statement cites. Use null (or []) if none apply."
        ),
    )
    confidence: float = Field(ge=0, le=1, description="Self-rated extraction confidence in [0,1].")
    applies_to_healthcare: bool = Field(
        description="True iff the scope provision is operationally relevant to MigrainePredict."
    )


# --- Stage 1 output ---


class StatementCandidate(BaseModel):
    statement_class: StatementClass = Field(
        description="DEONTIC, DEFINITIONAL, APPLICABILITY, or NOT_APPLICABLE."
    )
    anchor: str = Field(
        description=(
            "A short (≤20 words) snippet or paraphrase identifying which part "
            "of the paragraph this candidate refers to. For NOT_APPLICABLE, "
            "use '—'."
        )
    )
    rationale: str = Field(
        description="One sentence (≤25 words) justifying the classification."
    )


class ParagraphClassification(BaseModel):
    """Stage 1 output. List one candidate per distinct statement the paragraph
    carries. If nothing is extractable, emit a single NOT_APPLICABLE candidate."""

    candidates: list[StatementCandidate] = Field(
        description=(
            "One candidate per distinct statement found. If the paragraph "
            "carries no extractable statement, emit exactly one candidate with "
            "class NOT_APPLICABLE."
        )
    )


# ---------------------------------------------------------------------------
# Prompts (Chain-of-Instructions layout, per Galli)
# ---------------------------------------------------------------------------

_METHODS_SECTION = """\
## Element-Extraction Methods

Each ExtractedValue carries a `method` field. Assign exactly one method per
element, in priority order:

1. **STATED** — the element appears in the paragraph text itself.
2. **CONTEXT** — the element is drawn from the parent chain or a sibling
   paragraph of the same unit (article / annex / recital).
3. **CITATION** — the element is drawn from a cross-referenced provision whose
   text is bundled below.
4. **NONE** — the element is absent or unsupported. `value` must be null.

## No Inference from Silence

If the bundled context (paragraph + parent + siblings + cited provisions) does
not support an element, set its method to NONE and its value to null. Do NOT
fill values from general knowledge. This rule overrides every other
consideration."""

_COMMON_OUTPUT_FIELDS = """\
## Common Output Fields

- **source_article**: copy the paragraph IRI from the prompt verbatim.
- **references**: pick the subset of the *Resolved cross-reference IRIs* listed
  in the prompt that this specific statement cites. Use null or [] if none.
- **confidence**: float in [0,1]; self-rated extraction confidence.
- **applies_to_healthcare**: true iff the statement is operationally relevant
  to MigrainePredict — i.e., touches biometric/health data, high-risk AI under
  Annex III or safety-component-of-medical-device, obligations on providers /
  deployers of high-risk AI, or data-subject rights to health-related
  processing. Otherwise false."""


STAGE1_SYSTEM_PROMPT = f"""\
# Statement-Class Classifier for EU Regulatory Paragraphs

## Overview

You classify EU regulatory paragraphs (GDPR, AI Act) for downstream structured
extraction. You enumerate the distinct statements the paragraph carries and
assign each one to a class: DEONTIC, DEFINITIONAL, APPLICABILITY, or
NOT_APPLICABLE.

A paragraph can carry multiple statements, possibly of mixed classes. Examples:
GDPR Art 5(1) lists six DEONTIC principles; GDPR Art 4 packs one DEFINITIONAL
statement per numbered point; some paragraphs carry both a DEONTIC obligation
and an APPLICABILITY condition.

## Classes

- **DEONTIC** — an obligation, permission, prohibition, or dispensation
  addressed to a regulatory subject (controller, processor, provider, deployer,
  Member State, etc.). Look for modal verbs: "shall", "must", "may", "shall not".
- **DEFINITIONAL** — defines a regulatory term (e.g., GDPR Art 4 definitions,
  AI Act Art 3 definitions, or a recital supplying definitional criteria such
  as identifiability tests).
- **APPLICABILITY** — a material, territorial, personal, or temporal scope
  clause. Phrasings: "this Regulation applies to…", "this Regulation does not
  apply where…", "shall apply only to…".
- **NOT_APPLICABLE** — the paragraph contains none of the above (preamble,
  internal regulatory machinery, bare cross-reference, commencement language,
  or otherwise carries nothing extractable).

## Instructions

1. Read the paragraph together with its parent chain (lead-in clauses above)
   and its sibling paragraphs.
2. Identify each distinct statement the paragraph carries.
3. For each statement, assign exactly one class.
4. Write a short `anchor` (≤20 words) that identifies which part of the
   paragraph the candidate refers to — a snippet or paraphrase. For
   NOT_APPLICABLE, use '—'.
5. Write a one-sentence `rationale` (≤25 words) for the classification.
6. If the paragraph carries multiple statements, emit one candidate per
   statement.
7. If the paragraph carries nothing extractable, emit a single candidate with
   class NOT_APPLICABLE.

## No Inference from Silence

Do not project a statement that is not supported by the paragraph or its
context. If the paragraph is mere preamble, motivation, or commencement
language, classify as NOT_APPLICABLE rather than inventing a DEONTIC reading."""


STAGE1_USER_PROMPT = """\
{context_bundle}

Classify each distinct statement in the paragraph above."""


STAGE2_DEONTIC_SYSTEM_PROMPT = f"""\
# Deontic Statement Extractor (EU Regulatory Paragraph)

## Overview

You extract the structured representation of a single DEONTIC statement
(obligation, permission, prohibition, or dispensation) from an EU regulatory
paragraph. You receive the paragraph plus its parent chain, sibling
paragraphs, and any cross-referenced provisions whose text is bundled, and an
`anchor` pointing to which statement in the paragraph to extract.

## Core Distinction (per Galli)

- **Obligation of being**: duty to maintain or achieve a state ("shall be
  secure by design"). Addressee is often implicit; predicate is passive.
- **Obligation of action**: duty requiring concrete steps ("shall perform a
  risk assessment"). Addressee is explicit; predicate is active or passive.

This distinction guides predicate extraction; both forms produce a
DeonticStatement.

## Schema

- **modality**: OBLIGATION, PERMISSION, PROHIBITION, or DISPENSATION.
- **subject** (multi-valued): the addressee(s) on whom the modality acts.
- **predicate** (multi-valued): the deontic action verb phrase(s). For an
  obligation of being the predicate is passive; for an obligation of action
  the predicate is active or passive.
- **object** (multi-valued): the target(s) the predicate acts on. Must differ
  from subject.
- **condition** (optional, single): preconditions or qualifying circumstances.
  Null if absent.
- **beneficiary** (optional, single): the party benefiting from the statement.
  Null if absent.

## Multiple Values Per Element

Subject, predicate, and object are lists. A single statement may have multiple
addressees ("controllers and processors"), multiple verbs ("shall implement
and maintain"), or multiple targets. Emit one ExtractedValue per distinct
value. If a single statement would require fundamentally different predicates
acting on fundamentally different targets, that is two statements; the
classifier should have split them, so do not produce a merged extraction here.

{_METHODS_SECTION}

{_COMMON_OUTPUT_FIELDS}
- **severity**: high (fines-tier, market-blocking, or conditioning lawfulness
  of processing / placement on the market), medium (procedural / documentation
  / notification), or low (housekeeping).

## Instructions

1. Locate the statement indicated by the `anchor`.
2. Determine the modality from the modal verb ("shall" → obligation/prohibition
   depending on negation; "may" → permission; "need not" / "is not required
   to" → dispensation).
3. Extract subject, predicate, and object as multi-valued lists, each entry an
   ExtractedValue with method assigned per the priority above.
4. Fill condition and beneficiary if supported by the bundled context;
   otherwise set them to null.
5. Set source_article from the prompt and pick the references subset.
6. Assess applies_to_healthcare and severity per the rubrics above.
7. Self-rate confidence in [0,1]."""


STAGE2_DEFINITIONAL_SYSTEM_PROMPT = f"""\
# Definitional Statement Extractor (EU Regulatory Paragraph)

## Overview

You extract the structured representation of a single DEFINITIONAL statement
from an EU regulatory paragraph. The paragraph may define a regulatory term
(GDPR Art 4, AI Act Art 3) or supply definitional criteria for one (recitals
that elaborate an identifiability test, a scope-determining factor, etc.).
You receive the paragraph plus its bundled context and an `anchor` pointing
to which definition in the paragraph to extract.

## Schema

- **term**: the term being defined, in canonical form (e.g., 'personal data',
  not 'data' — match the regulation's wording).
- **definition**: an ExtractedValue carrying the definitional text + method.

{_METHODS_SECTION}

{_COMMON_OUTPUT_FIELDS}

## Instructions

1. Locate the definition indicated by the `anchor`.
2. Set `term` to the defined term as written in the regulation.
3. Set `definition.value` to the definitional text and assign its method per
   the priority above. If the definition is the paragraph's own text, method
   is STATED.
4. Set source_article from the prompt and pick the references subset.
5. Assess applies_to_healthcare per the rubric above.
6. Self-rate confidence in [0,1]."""


STAGE2_APPLICABILITY_SYSTEM_PROMPT = f"""\
# Applicability Statement Extractor (EU Regulatory Paragraph)

## Overview

You extract the structured representation of a single APPLICABILITY statement
from an EU regulatory paragraph. Applicability statements set or restrict the
regulation's scope on one of four axes: MATERIAL (subject-matter), TERRITORIAL
(geographic), PERSONAL (who the regulation binds), or TEMPORAL (when it
applies). You receive the paragraph plus its bundled context and an `anchor`
pointing to which scope clause to extract.

## Schema

- **scope_type**: MATERIAL, TERRITORIAL, PERSONAL, or TEMPORAL.
- **applies_to**: the entity, activity, or class the scope qualifies.
- **condition**: the condition that triggers applicability or exclusion.
- **polarity**: INCLUDES (brings within scope) or EXCLUDES (places outside
  scope).

{_METHODS_SECTION}

{_COMMON_OUTPUT_FIELDS}

## Instructions

1. Locate the scope clause indicated by the `anchor`.
2. Determine the scope axis from the phrasing ("processing of personal data"
   → MATERIAL; "established in the Union" → TERRITORIAL; "controllers and
   processors" → PERSONAL; "from <date>" / "until <date>" → TEMPORAL).
3. Determine polarity from the phrasing ("applies to" / "applies where" →
   INCLUDES; "does not apply to" / "shall not apply where" → EXCLUDES).
4. Extract applies_to and condition as ExtractedValues with method assigned
   per the priority above.
5. Set source_article from the prompt and pick the references subset.
6. Assess applies_to_healthcare per the rubric above.
7. Self-rate confidence in [0,1]."""


STAGE2_USER_PROMPT = """\
{context_bundle}

Anchor (the statement to extract): {anchor}

Extract the structured representation of the indicated statement."""


# ---------------------------------------------------------------------------
# Context bundle
# ---------------------------------------------------------------------------


def build_context_bundle(rec: dict) -> str:
    """Render a single post-screened paragraph and its bundled context as the
    user-prompt body. The bundle exposes paragraph text, ancestor lead-ins,
    immediate siblings, and the resolved text of any same-corpus cross-refs
    whose targets were screen-kept."""
    parts: list[str] = []
    parts.append(f"Paragraph IRI: {rec['iri']}")

    src_line = f"Source: {rec['source']} {rec['unit_type']} {rec['unit_number']}"
    if rec.get("heading"):
        src_line += f" — {rec['heading']}"
    parts.append(src_line)

    parts.append("")
    parts.append("Paragraph text:")
    parts.append(rec["text"])

    parent = rec.get("parent") or []
    if parent:
        parts.append("")
        parts.append("Parent chain (lead-in clauses above this paragraph):")
        for p in parent:
            iri = p.get("iri") or "—"
            parts.append(f"  [{iri}] {p['text']}")

    for label, key in [("Previous", "previous_sibling"), ("Next", "next_sibling")]:
        sib = rec.get(key)
        if not sib:
            continue
        keep_tag = "kept" if sib.get("screen_keep") else "dropped"
        parts.append("")
        parts.append(f"{label} sibling [{sib['iri']}] ({keep_tag}):")
        parts.append(sib["text"])

    crefs = rec.get("cross_references") or []
    if crefs:
        parts.append("")
        parts.append("Cross-referenced provisions:")
        for ref in crefs:
            raw = ref.get("raw")
            riri = ref.get("resolved_iri")
            kind = ref.get("kind")
            text = ref.get("text")
            if riri and text:
                parts.append(f"  [{riri}] ({kind}, raw=\"{raw}\"):")
                parts.append(f"    {text}")
            elif riri:
                parts.append(
                    f"  [{riri}] ({kind}, raw=\"{raw}\") — referenced but text "
                    "not bundled (cross-corpus or screen-dropped target)"
                )
            else:
                parts.append(f"  (raw=\"{raw}\", {kind}) — unresolved")

        resolved = [r["resolved_iri"] for r in crefs if r.get("resolved_iri")]
        if resolved:
            parts.append("")
            parts.append("Resolved cross-reference IRIs available for the `references` field:")
            for iri in resolved:
                parts.append(f"- {iri}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM + chains
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Read .env into os.environ for keys not already set. Lightweight to avoid
    a hard dependency on python-dotenv (mirrors screen.py)."""
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


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


def build_chains(backend: str):
    """Build the four chains used per paragraph: stage-1 classifier and three
    typed stage-2 extractors (one per non-NA class)."""
    llm = _llm(backend)
    cls_chain = ChatPromptTemplate.from_messages(
        [("system", STAGE1_SYSTEM_PROMPT), ("user", STAGE1_USER_PROMPT)]
    ) | llm.with_structured_output(ParagraphClassification)
    deontic_chain = ChatPromptTemplate.from_messages(
        [("system", STAGE2_DEONTIC_SYSTEM_PROMPT), ("user", STAGE2_USER_PROMPT)]
    ) | llm.with_structured_output(DeonticStatement)
    definitional_chain = ChatPromptTemplate.from_messages(
        [("system", STAGE2_DEFINITIONAL_SYSTEM_PROMPT), ("user", STAGE2_USER_PROMPT)]
    ) | llm.with_structured_output(DefinitionalStatement)
    applicability_chain = ChatPromptTemplate.from_messages(
        [("system", STAGE2_APPLICABILITY_SYSTEM_PROMPT), ("user", STAGE2_USER_PROMPT)]
    ) | llm.with_structured_output(ApplicabilityStatement)
    return cls_chain, deontic_chain, definitional_chain, applicability_chain


# ---------------------------------------------------------------------------
# Per-paragraph orchestration
# ---------------------------------------------------------------------------


def _na_stub(iri: str, rationale: str, *, error: str | None = None) -> dict:
    """Build a NOT_APPLICABLE ExtractionResult record."""
    out = {
        "statement_class": StatementClass.NOT_APPLICABLE.value,
        "statement": None,
        "paragraph_iri": iri,
        "needs_review": error is not None,
        "classification_rationale": rationale,
    }
    if error is not None:
        out["extractor_error"] = error
    return out


def _process_paragraph(chains, rec: dict) -> tuple[dict, list[dict], bool]:
    """Run the full two-stage pipeline on one paragraph. Returns (rec,
    list-of-result-records, errored). On stage-1 failure or unexpected empty
    classification, emits a NOT_APPLICABLE stub flagged needs_review=True so
    nothing is silently dropped."""
    cls_chain, deontic_chain, definitional_chain, applicability_chain = chains
    ctx = build_context_bundle(rec)
    iri = rec["iri"]

    try:
        classification: ParagraphClassification = cls_chain.invoke({"context_bundle": ctx})
    except Exception as e:
        return rec, [_na_stub(iri, "stage1 failure", error=str(e))], True

    if not classification.candidates:
        return rec, [_na_stub(iri, "stage1 returned empty list", error="empty candidates")], True

    results: list[dict] = []
    errored = False
    for cand in classification.candidates:
        cls = cand.statement_class
        if cls == StatementClass.NOT_APPLICABLE:
            results.append(_na_stub(iri, cand.rationale))
            continue
        if cls == StatementClass.DEONTIC:
            stage2 = deontic_chain
        elif cls == StatementClass.DEFINITIONAL:
            stage2 = definitional_chain
        elif cls == StatementClass.APPLICABILITY:
            stage2 = applicability_chain
        else:
            results.append(_na_stub(iri, f"unknown class {cls}", error=f"unknown class {cls}"))
            errored = True
            continue

        try:
            stmt = stage2.invoke({"context_bundle": ctx, "anchor": cand.anchor})
        except Exception as e:
            results.append({
                "statement_class": cls.value,
                "statement": None,
                "paragraph_iri": iri,
                "needs_review": True,
                "classification_rationale": cand.rationale,
                "anchor": cand.anchor,
                "extractor_error": str(e),
            })
            errored = True
            continue

        # Belt-and-suspenders: LLM may copy source_article from the prompt, but
        # if it drifts we override with the input IRI (which is authoritative).
        stmt_dict = stmt.model_dump()
        if stmt_dict.get("source_article") != iri:
            stmt_dict["source_article"] = iri

        results.append({
            "statement_class": cls.value,
            "statement": stmt_dict,
            "paragraph_iri": iri,
            "needs_review": stmt.confidence < HITL_THRESHOLD,
            "classification_rationale": cand.rationale,
            "anchor": cand.anchor,
        })

    return rec, results, errored


# ---------------------------------------------------------------------------
# Batched driver
# ---------------------------------------------------------------------------


def iter_paragraphs(path: Path, limit: int | None) -> Iterator[dict]:
    n = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)
            n += 1
            if limit is not None and n >= limit:
                return


def extract(path: Path, limit: int | None, backend: str) -> Path:
    suffix = ".extracted.smoke.jsonl" if limit else ".extracted.jsonl"
    out_path = path.with_name(path.name.replace(".postscreened.jsonl", suffix))
    chains = build_chains(backend)

    work = list(iter_paragraphs(path, limit))
    total = len(work)
    n_batches = max(1, (total + BATCH_SIZE - 1) // BATCH_SIZE)

    n_results = 0
    n_paragraphs_errored = 0
    with out_path.open("w", encoding="utf-8") as dst:
        for b_idx, batch_start in enumerate(range(0, total, BATCH_SIZE)):
            batch = work[batch_start:batch_start + BATCH_SIZE]
            slots: list[tuple[dict, list[dict], bool] | None] = [None] * len(batch)
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
                fut_to_idx = {
                    ex.submit(_process_paragraph, chains, rec): i
                    for i, rec in enumerate(batch)
                }
                with tqdm(total=len(batch), desc=f"{path.name} batch {b_idx+1}/{n_batches}") as bar:
                    try:
                        for fut in as_completed(fut_to_idx, timeout=BATCH_DEADLINE_SECONDS):
                            slots[fut_to_idx[fut]] = fut.result()
                            bar.update(1)
                    except FuturesTimeout:
                        n_stuck = sum(1 for s in slots if s is None)
                        print(
                            f"  WARN: {n_stuck} paragraphs didn't complete within "
                            f"{BATCH_DEADLINE_SECONDS}s; flushing partial batch"
                        )

            for i, s in enumerate(slots):
                if s is None:
                    rec = batch[i]
                    s = (
                        rec,
                        [_na_stub(rec["iri"], "batch deadline exceeded",
                                  error="batch deadline exceeded")],
                        True,
                    )
                    slots[i] = s
                _rec, recs, errored = s  # type: ignore[misc]
                if errored:
                    n_paragraphs_errored += 1
                for r in recs:
                    dst.write(json.dumps(r, ensure_ascii=False) + "\n")
                    n_results += 1
            dst.flush()

            if batch_start + BATCH_SIZE < total:
                print(f"  sleeping {BATCH_WAIT_SECONDS}s before next batch")
                time.sleep(BATCH_WAIT_SECONDS)

    print(
        f"{path.name}: {total} paragraphs -> {n_results} extraction records "
        f"({n_paragraphs_errored} paragraphs with errors) -> {out_path}"
    )
    return out_path


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--limit", type=int, default=None,
        help="Stop after N paragraphs per file (smoke test). Writes to *.extracted.smoke.jsonl.",
    )
    p.add_argument(
        "--backend", default="gemini", choices=["gemini", "mistral"],
        help="LLM backend (default: gemini = Gemini 2.5 Flash via API).",
    )
    p.add_argument(
        "paths", nargs="*",
        help="Post-screened JSONL files; defaults to both data/ postscreened files.",
    )
    args = p.parse_args(argv)

    paths = [Path(x) for x in args.paths] if args.paths else DEFAULT_PATHS
    for i, path in enumerate(paths):
        if i > 0:
            print(f"\nsleeping {BATCH_WAIT_SECONDS}s between files (rate-limit pacing)")
            time.sleep(BATCH_WAIT_SECONDS)
        if not path.exists():
            print(f"warning: {path} not found, skipping")
            continue
        extract(path, args.limit, args.backend)


if __name__ == "__main__":
    main(sys.argv[1:])
