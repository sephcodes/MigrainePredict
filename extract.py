"""
Two-stage LLM extraction of deontic / definitional / applicability statements
from post-screened regulatory paragraphs (GDPR, EU AI Act).

The single source of truth for extraction conventions is
docs/annotation_guide.md. The Pydantic Field descriptions and CoI prompt
sections in this file mirror that guide; when a rule changes, edit the guide
AND the matching section here.

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

# MigrainePredict-profile keyword sets used by the rule-based
# applies_to_healthcare gate. A statement whose flattened text content does
# NOT match at least one keyword across these dimensions has its hc flag
# overridden to False with needs_review=True — even if the LLM said True.
# Matched substring-wise on lowercased text. Tune cautiously: every entry
# expands the set of provisions the gate will pass through.
PROFILE_KEYWORDS = {
    "lawful_basis": [
        "consent", "lawful", "lawfulness", "fairness", "transparen",
        "legitimate interest", "vital interest", "contract", "legal obligation",
        "public task", "necessary for",
    ],
    "data_categories": [
        "personal data", "data subject", "identifiable",
        "biometric", "genetic", "health", "sensitive", "special categor",
        "processing", "controller", "processor",
    ],
    "ai_act_risk_vector": [
        "high-risk", "high risk", "ai system", "biometric identification",
        "medical device", "safety component", "annex iii", "annex i",
        "provider", "deployer", "conformity assessment", "risk management",
        "post-market", "technical documentation",
    ],
}


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
            "Addressee(s) — the DUTY-BEARER role the modality binds. "
            "Allowed values are regulatory actor roles: 'the controller', "
            "'the processor', 'providers', 'deployers', 'Member States', "
            "'supervisory authorities', 'data subjects'. The subject is "
            "NEVER the grammatical subject of a passive sentence. "
            "'Personal data shall be processed' has subject='the "
            "controller' (CONTEXT, inferred from passive voice), NOT "
            "'Personal data' — the data goes in `object`. Method is "
            "STATED iff the actor is explicitly named in the paragraph; "
            "CONTEXT otherwise. Multi-valued for coordinated addressees "
            "('controllers and processors'). See annotation_guide.md §3.1."
        )
    )
    predicate: list[ExtractedValue] = Field(
        description=(
            "Deontic action verb phrase(s) in ACTIVE voice. When the source "
            "is passive ('Personal data shall be processed lawfully'), "
            "transform to the active form ('shall process lawfully') so "
            "subject and predicate compose: '<the controller> <shall "
            "process lawfully>'. Method is CONTEXT when the active form was "
            "transformed from a passive source; STATED when the source was "
            "already active. Manner adverbs ('lawfully', 'fairly', 'in a "
            "transparent manner') stay with the predicate — they describe "
            "HOW the action is performed, not WHEN it applies. Multi-valued "
            "for coordinated predicates sharing a subject and object. See "
            "annotation_guide.md §3.1, §3.3."
        )
    )
    object: list[ExtractedValue] = Field(
        description=(
            "Target(s) the predicate acts on. In passive-voice sources the "
            "grammatical subject moves here ('Personal data shall be "
            "processed' → object='personal data'). Must differ from "
            "subject. Multi-valued. Method tracks the SOURCE of the value "
            "text, NOT the slot: 'personal data' relocated from the source's "
            "grammatical subject is STATED here because the text appears in "
            "the paragraph. See annotation_guide.md §3.1."
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
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Subset of the resolved cross-reference IRIs listed in the prompt "
            "that this specific statement cites. Use [] if none apply."
        ),
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="Self-rated extraction confidence in [0,1].",
    )
    applies_to_healthcare: bool = Field(
        description=(
            "True iff MigrainePredict's controller must satisfy this "
            "provision when operating the system. MigrainePredict is a "
            "wearable healthcare AI that processes biometric / health data, "
            "performs processing on personal data of identifiable natural "
            "persons, and is a high-risk AI system on the medical-device "
            "pathway (Annex III §1 / AI Act Art 6(1)). Mark TRUE for: "
            "foundational definitions of personal data, processing, "
            "identifiable natural person; general processing principles (Art "
            "5); lawful basis (Art 6, Art 9); data-subject rights (Arts "
            "12-22); controller obligations (Arts 24, 25, 30, 32, 35); "
            "DPO/DPIA provisions; AI Act high-risk requirements; AI Act "
            "provider / deployer obligations. Mark FALSE for provisions "
            "specific to other sectors (employment Art 88, journalism Art 85, "
            "research Art 89, public authorities Art 86, religious "
            "associations Art 91), internal regulatory machinery (Commission "
            "powers, delegated acts), and final / commencement provisions."
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
        description=(
            "The COMPLETE definitional clause + extraction method. MUST "
            "include every 'which …' / 'that …' / 'such as …' sub-clause "
            "qualifying the term. Do NOT truncate at internal commas or "
            "relative-pronoun boundaries — the operative content of EU "
            "definitions is often in the second half of the clause (e.g. "
            "AI Act Art 3(1)'s inference clause; GDPR Art 4(14)'s 'unique "
            "identification' clause). The clause ends at the semicolon that "
            "marks end-of-definition in EU regulatory drafting. See "
            "annotation_guide.md §4.1."
        )
    )
    source_article: str = Field(
        description="The canonical paragraph IRI given in the prompt — copy it verbatim."
    )
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Subset of the resolved cross-reference IRIs listed in the prompt "
            "that this definition cites. Use [] if none apply."
        ),
    )
    confidence: float = Field(ge=0, le=1, description="Self-rated extraction confidence in [0,1].")
    applies_to_healthcare: bool = Field(
        description=(
            "True iff MigrainePredict's controller must reason with this "
            "definition when operating the system. MigrainePredict processes "
            "biometric / health data of identifiable natural persons and is a "
            "high-risk AI system on the medical-device pathway. Mark TRUE for "
            "foundational definitions used throughout MP's compliance posture "
            "('personal data', 'processing', 'identifiable natural person', "
            "'controller', 'processor', 'biometric data', 'genetic data', "
            "'data concerning health', 'special category', 'AI system', "
            "'high-risk AI system', 'provider', 'deployer'). Mark FALSE for "
            "terms specific to other sectors or contexts MP does not engage "
            "in (e.g. journalistic purposes, religious processing, "
            "public-authority-specific roles)."
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
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Subset of the resolved cross-reference IRIs listed in the prompt "
            "that this statement cites. Use [] if none apply."
        ),
    )
    confidence: float = Field(ge=0, le=1, description="Self-rated extraction confidence in [0,1].")
    applies_to_healthcare: bool = Field(
        description=(
            "True iff the scope clause brings MigrainePredict's "
            "operations / data within (INCLUDES) or outside (EXCLUDES) the "
            "regulation's reach. MigrainePredict processes biometric / health "
            "data of natural persons in the Union and is a high-risk AI "
            "system on the medical-device pathway (Annex III §1 / AI Act Art "
            "6(1)). Mark TRUE for any scope clause whose `applies_to` or "
            "`condition` overlaps MP's processing context — including general "
            "MATERIAL scope on personal data processing, TERRITORIAL scope "
            "covering EU controllers / data subjects, PERSONAL scope on "
            "natural persons, and the high-risk classification rules. Mark "
            "FALSE for scope carveouts to sectors MP does not engage in "
            "(employment, journalism, research, religious processing, public "
            "authorities) and for internal regulatory machinery."
        )
    )


# --- Stage 1 output ---


class StatementCandidate(BaseModel):
    statement_class: StatementClass = Field(
        description="DEONTIC, DEFINITIONAL, APPLICABILITY, or NOT_APPLICABLE."
    )
    anchor: str = Field(
        description=(
            "A short (≤20 words) snippet (verbatim from the paragraph text, "
            "preferred) or close paraphrase identifying which part of the "
            "paragraph this candidate refers to. Required for ALL classes "
            "including NOT_APPLICABLE — for NA records the anchor becomes "
            "the audit text. Do not emit placeholders like '—'."
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
  Member State, etc.). Look for modal verbs: "shall", "must", "may", "shall
  not". **DEONTIC is reserved for ARTICLE and ANNEX paragraphs.** Recitals are
  non-binding interpretive guidance and do not produce DEONTIC statements,
  even when they contain "should" / "may" language — classify recital
  exhortations as NOT_APPLICABLE, or as DEFINITIONAL / APPLICABILITY if they
  supply definitional criteria or a scope test.
- **DEFINITIONAL** — defines a regulatory term (e.g., GDPR Art 4 definitions,
  AI Act Art 3 definitions, or a recital supplying definitional criteria such
  as identifiability tests). The paragraph must contain ACTUAL definitional
  content (intensional content describing what the term means or what
  properties it has). Two patterns are NOT DEFINITIONAL even if they look like
  definitions:
    - **Pointer-only**: the only content is a reference to an external source
      (e.g., "the notion of X should draw from Article 2 of Recommendation
      Y"). Classify as NOT_APPLICABLE.
    - **Forward-reference / empty connective**: the term is "defined" by a
      bare list-introducer that points elsewhere in the regulation (e.g., "X
      shall be considered to be high-risk where the following conditions are
      fulfilled", "X are the AI systems listed in the following areas", "X as
      defined in Article N"). The "definition" carries no intensional content
      and is just a connective. Classify as APPLICABILITY (with scope_type =
      MATERIAL, polarity = INCLUDES, and the condition expanded from the
      connective).
- **APPLICABILITY** — a material, territorial, personal, or temporal scope
  clause. Phrasings: "this Regulation applies to…", "this Regulation does not
  apply where…", "shall apply only to…".
- **NOT_APPLICABLE** — the paragraph contains none of the above (preamble,
  internal regulatory machinery, pointer-only references to external
  definitions, bare cross-reference, commencement language, recital "should"
  language that is interpretive guidance rather than substantive criteria, or
  otherwise carries nothing extractable).

## Instructions

1. Read the paragraph together with its parent chain (lead-in clauses above)
   and its sibling paragraphs. Note its `Source:` line — if `unit_type` is
   `recital`, DEONTIC is forbidden.
2. Identify each distinct statement the paragraph carries.
3. For each statement, assign exactly one class.
4. Write a short `anchor` (≤20 words) that identifies which part of the
   paragraph the candidate refers to — a verbatim snippet from the paragraph
   text (preferred) or a close paraphrase. This applies to NOT_APPLICABLE
   candidates too: the anchor is what makes the NA record auditable
   downstream. Do NOT use a placeholder like '—' — emit the actual sentence
   or snippet that you classified as non-extractable.
5. Write a one-sentence `rationale` (≤25 words) for the classification.
   Forbidden vocabulary in the rationale: the words "obligation",
   "permission", "prohibition", "dispensation". These name SPECIFIC
   modalities determined at stage 2, not at stage 1. Use class-level and
   neutral structural language: "DEONTIC exception", "DEONTIC carve-out",
   "DEONTIC derogation", "DEONTIC processing principle", "DEONTIC controller
   duty", "DEFINITIONAL of a regulatory term", "APPLICABILITY of TERRITORIAL
   scope". A rationale that says "DEONTIC dispensation" will contradict
   stage 2 when the parent rule turns out to be a PROHIBITION (in which case
   modality = PERMISSION, not DISPENSATION).
6. If the paragraph carries multiple statements, emit one candidate per
   statement.
7. If the paragraph carries nothing extractable, emit a single candidate with
   class NOT_APPLICABLE.

## Exemption Clauses Are DEONTIC, Not APPLICABILITY

Clauses of the form "X shall not apply where Y" / "X shall apply only where Y"
attached to a parent rule are operative exceptions, NOT freestanding scope
clauses. Classify them by the parent rule they modify:

- Exception to a PROHIBITION (e.g., Art 9(2) carveouts to the Art 9(1)
  special-category ban) → DEONTIC (the carveout grants a PERMISSION).
- Exception to an OBLIGATION (e.g., a derogation from a "shall" duty) →
  DEONTIC (the carveout is a DISPENSATION).

Only true scope clauses — those defining the regulation's overall MATERIAL,
TERRITORIAL, PERSONAL, or TEMPORAL reach (Art 2, Art 3) — are APPLICABILITY.

## Predicate-Object Pairing — One Statement Per Pair

When a paragraph contains multiple deontic predicates with INDEPENDENT objects
(each predicate acts on a distinct object/condition, not a shared one), emit
one DEONTIC candidate per predicate-object pair, not a single merged
candidate. Example: "Personal data shall be collected for specified purposes
and not further processed in a manner incompatible with those purposes" — two
candidates: ("shall be collected", "for specified purposes") and ("shall not
be further processed", "in a manner incompatible with those purposes").

Merging into a single candidate with parallel-list predicates and objects is
permitted ONLY when the predicates share the same object (or vice versa) —
e.g., "shall implement and maintain appropriate measures" (two predicates,
one shared object → one candidate, predicate is multi-valued in stage 2).
Independent pairs MUST be split.

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
- **subject** (multi-valued): the DUTY-BEARER role (see "Subject Convention"
  below) — NOT the grammatical subject of the sentence.
- **predicate** (multi-valued): the deontic action verb phrase(s). Stays in
  the form the regulation uses (passive when the source is passive).
- **object** (multi-valued): the target(s) the predicate acts on. In passive
  constructions the grammatical subject becomes the object.
- **condition** (optional, single): preconditions or qualifying circumstances.
  Null if absent.
- **beneficiary** (optional, single): the party benefiting from the statement.
  Null if absent.

## Active-Voice Convention — Frozen per Guide §3.1

DEONTIC statements are normalised to **active voice throughout**. Subject and
predicate compose: read your output as `<subject> <predicate> <object>` and
verify it is a coherent active-voice clause.

- **subject** = the duty-bearer role (the controller, the processor,
  providers, deployers, Member States, supervisory authorities, data
  subjects). NEVER the grammatical subject of a passive source.
- **predicate** = the deontic verb phrase in ACTIVE voice. When the
  regulation's surface form is passive, transform to active. The grammatical
  subject of the source passive moves to `object`; the implicit agent of
  the passive becomes `subject`.

Worked transformations (source → extracted):

| Source text | subject | predicate | object |
| --- | --- | --- | --- |
| "Personal data shall be processed lawfully" | "the controller" (CONTEXT) | "shall process lawfully" (CONTEXT) | "personal data" (STATED) |
| "Personal data shall be collected for specified purposes" | "the controller" (CONTEXT) | "shall collect for specified purposes" (CONTEXT) | "personal data" (STATED) |
| "Personal data shall not be further processed in an incompatible manner" | "the controller" (CONTEXT) | "shall not further process in an incompatible manner" (CONTEXT) | "personal data" (STATED) |
| "Processing of special categories shall be prohibited" | "the controller" (CONTEXT) | "shall not process" (CONTEXT) | "special categories of personal data" (STATED) |
| "Controllers shall implement appropriate measures" | "controllers" (STATED) | "shall implement" (STATED) | "appropriate measures" (STATED) |
| "Data subjects shall have the right to access" | "data subjects" (STATED) | "shall have" (STATED) | "the right to access" (STATED) |

### Method Rule (CRITICAL)

Method marks the **SOURCE** of the value text, NOT which schema slot it
landed in.

- **STATED** when the value's text (or close paraphrase) appears in the
  paragraph itself — INCLUDING a value relocated from the grammatical
  subject of a passive source to `object`. "Personal data" in `object` is
  STATED because the text "Personal data" literally appears in the
  paragraph, even though we moved it out of the surface subject slot.
- **CONTEXT** when the value was inferred from passive-voice transformation
  ("shall process" derived from "shall be processed" — the active form is
  not literally in the paragraph), drawn from a sibling paragraph, or
  pulled from the parent chapeau.
- **CITATION** when the value was lifted from a cross-referenced provision
  whose text is bundled in the prompt.

If you find yourself marking a slot CONTEXT when the value text literally
appears in the paragraph, you have applied the rule wrong — check whether
the text is in the paragraph first; if yes, STATED.

### Manner Adverbs Belong in the Predicate

How-descriptions (lawfully, fairly, in a transparent manner, securely) stay
with the predicate, not the condition. Conditions describe WHEN / IF /
UNLESS the duty applies (preconditions, qualifying circumstances) — NOT how
it is performed.

- Right: predicate = "shall process lawfully, fairly and in a transparent
  manner", condition = null.
- Wrong: predicate = "shall process", condition = "lawfully, fairly and in
  a transparent manner".

Test: does the phrase change WHEN the duty applies, or just HOW it's
performed? When-changing → condition. How-describing → predicate.

## Multiple Values Per Element — Reserved for Coordinated Structures

Subject, predicate, and object are lists. Multi-valued emission is reserved
for genuinely coordinated structures where multiple values share the rest of
the statement:

- Multiple subjects sharing one predicate-object pair ("controllers and
  processors shall designate a representative")
- Multiple predicates sharing one object ("shall implement and maintain
  appropriate measures")
- Multiple objects sharing one predicate ("shall ensure the security and
  integrity of personal data")

Worked examples (prose form, since these prompts are interpolated):

- Input: "The controller shall implement appropriate technical and
  organisational measures and shall regularly review them"
  → subject: one ExtractedValue, value="the controller", method=STATED.
    predicate: TWO ExtractedValues — value="shall implement", method=STATED;
    value="shall regularly review", method=STATED.
    object: one ExtractedValue, value="appropriate technical and
    organisational measures", method=STATED.
    (Both predicates share the SAME object — "them" refers back to the
    measures — so this is one statement, multi-predicate.)

- Input: "Controllers and processors shall designate a representative"
  → subject: TWO ExtractedValues — value="controllers", method=STATED;
    value="processors", method=STATED.
    predicate: one ExtractedValue, value="shall designate", method=STATED.
    object: one ExtractedValue, value="a representative", method=STATED.

If a single statement would require predicates with INDEPENDENT objects (not
shared), the classifier should have split it into multiple candidates. Do NOT
emit parallel-list predicates and objects with index-aligned pairing — that
pairing is fragile and downstream consumers cannot recover it.

## Modality of Exemptions — Invert the Parent's Modality

When the statement is an exception attached to a parent rule, the exception's
modality is the INVERSE of the parent rule's modality. Do NOT default every
"shall not apply where" exception to DISPENSATION — read the parent first,
then flip.

- Parent is a PROHIBITION ("shall not", "is prohibited") → exception is
  PERMISSION (the carve-out ALLOWS what was prohibited).
- Parent is an OBLIGATION ("shall", "must") → exception is DISPENSATION (the
  carve-out RELEASES the addressee from the duty).

Worked example 1 — exception to a PROHIBITION:
- Parent rule (Art 9(1)): "Processing of personal data revealing racial or
  ethnic origin… shall be prohibited" → PROHIBITION.
- Exception (Art 9(2)(h)): "Paragraph 1 shall not apply if processing is
  necessary for the purposes of preventive or occupational medicine…"
- → modality = **PERMISSION** (not DISPENSATION).

Worked example 2 — exception to an OBLIGATION:
- Parent rule (Art 30(1)): "Each controller shall maintain a record of
  processing activities" → OBLIGATION.
- Exception (Art 30(5)): "Paragraphs 1 and 2 shall not apply to an enterprise
  employing fewer than 250 persons unless…"
- → modality = **DISPENSATION** (not PERMISSION).

If you cannot see the parent rule's modality in the bundled context, set
modality to PERMISSION when the parent is described as a prohibition in the
parent chapeau, otherwise DISPENSATION.

## Subject of an Exemption — Inherit from the Parent Rule

For a PERMISSION or DISPENSATION carve-out, the SUBJECT is the party RECEIVING
the permission (or being released from the duty) — typically the controller
or the processing itself, inherited from the parent rule's subject. The
subject is NOT a safeguard actor mentioned inside the carve-out (e.g., "a
professional subject to professional secrecy") — those are conditions on when
the carve-out applies, and belong in `condition`, not `subject`.

Worked example — Art 9(2)(h) carve-out to the Art 9(1) prohibition:
- Parent (Art 9(1)): "Processing of personal data revealing… shall be
  prohibited" — implicit subject is the controller processing such data.
- Carve-out (Art 9(2)(h)): "Paragraph 1 shall not apply if processing is
  necessary for the purposes of preventive or occupational medicine… on the
  basis of Union or Member State law or pursuant to contract with a health
  professional and subject to the conditions and safeguards referred to in
  paragraph 3."
- Correct extraction:
  - modality = PERMISSION
  - subject  = "the controller" (or "the processing") — method CONTEXT,
    drawn from the parent prohibition's implicit subject.
  - predicate = "may process" — method CONTEXT, inferred from inverting the
    parent prohibition.
  - object   = "special categories of personal data" — method CONTEXT, from
    the parent.
  - condition = full carve-out trigger: "processing is necessary for the
    purposes of preventive or occupational medicine, for the assessment of
    the working capacity of the employee, medical diagnosis, the provision
    of health or social care or treatment, or the management of health or
    social care systems and services… AND subject to professional secrecy
    and the safeguards in paragraph 3" — method STATED.
- Wrong extraction (do NOT produce this):
  - subject = "a professional subject to the obligation of professional
    secrecy" — that actor is a safeguard requirement, part of the condition,
    not the party the permission runs to. A Phase-2 query "may the controller
    process under Art 9(2)(h)?" must match this statement's subject node.

## Exemption References — Use the Parent Chain's `references cited`

If the modality is PERMISSION or DISPENSATION arising from a parent rule the
statement carves out, you MUST populate `references` with the parent rule's
IRI. The parent chain in the prompt lists ancestors as
`[<iri>] <text>` lines; when an ancestor is an exemption chapeau (e.g.,
"Paragraph 1 shall not apply"), its own resolved IRIs appear directly beneath
it on a "references cited in this lead-in:" line. Those IRIs ARE the
operative parent rules — copy them into `references`. Add any safeguards /
condition-source IRIs from the statement's own cross-references on top — both
belong, but the parent rule is mandatory.

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
- **definition**: an ExtractedValue carrying the COMPLETE definitional clause
  + method.

## Completeness — Extract the WHOLE Clause

`definition.value` MUST contain the **complete** definitional clause. Every
sub-clause introduced by "which", "that", "such as", "including",
"consisting of" is INSIDE the definition. Do NOT truncate at internal
commas, semicolons-before-another-sub-clause, or relative-pronoun
boundaries.

The legally load-bearing content of EU definitions is routinely in the
SECOND HALF of the clause. Truncating early produces a definition that no
longer matches the regulation:

- AI Act Art 3(1) "AI system": the full clause goes from "a machine-based
  system…" all the way through "…that can influence physical or virtual
  environments". The "infers, from the input it receives, how to generate
  outputs" segment IS the operative discriminator — it is what
  distinguishes an AI system from ordinary software. Cutting at "after
  deployment" produces the wrong definition.

- GDPR Art 4(14) "biometric data": the full clause goes from "personal data
  resulting from specific technical processing…" through "…such as facial
  images or dactyloscopic data". The "which allow or confirm the unique
  identification" segment IS the operative discriminator — it is what
  pulls biometric data into Art 9. Cutting at "natural person" produces
  the wrong definition.

- GDPR Art 4(15) "data concerning health": the full clause goes through
  "…which reveal information about his or her health status". The trailing
  clause is the operative discriminator.

**Stopping rule**: The definition ends at the semicolon that closes the
term's entry in the article. Internal commas and relative clauses are
INSIDE the definition. Internal semicolons that precede ANOTHER numbered
definition are the end of THIS one.

When the source paragraph packs multiple distinct definitions separated by
end-of-definition semicolons (e.g. GDPR Art 4(1) defines both "personal
data" AND "identifiable natural person"), emit one DEFINITIONAL candidate
per distinct term — but each candidate's `definition.value` carries the
full clause for its own term.

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
- **applies_to**: the entity, activity, or class the scope qualifies. This
  MUST be the *thing the scope bears on* — a domain, data-type, activity,
  product class, or category — NOT an actor or role. If the candidate's
  applies_to comes out as an actor ("Member States", "controllers", "the
  Commission"), re-examine: the actor is usually the *agent* of the scope
  rule, while the *thing scoped* is the rules, the processing, or the
  category being qualified. Set applies_to to the latter.
- **condition**: the condition that triggers applicability or exclusion.
- **polarity**: INCLUDES (brings within scope) or EXCLUDES (places outside
  scope).

## Scope-Type Heuristics

Use the dominant phrasing of the clause to pick scope_type:

- **TERRITORIAL** — when the trigger is establishment, residence, location,
  or geographic context ("established in the Union", "regardless of whether
  the processing takes place in the Union", "for the offering of goods or
  services to data subjects in the Union"). Article 3 of GDPR is the canonical
  case; do not let an incidental mention of "natural persons" pull the
  classification to PERSONAL.
- **MATERIAL** — when the trigger is a data type or a processing activity
  ("processing of personal data", "the placing on the market of high-risk AI
  systems", "automated decision-making").
- **PERSONAL** — when the trigger is the natural-vs-legal-person distinction
  or a class of natural persons ("natural persons", "children", "data
  subjects"). PERSONAL is about WHO the regulation binds or protects, not
  WHERE they are.
- **TEMPORAL** — when the trigger is a date, transition period, or
  application timeline ("from <date>", "until <date>", "during the
  transitional period").

If multiple axes are present, pick the one the clause's verb is conditioning
on, not the one most prominently mentioned.

## List-Introducer Conditions — Reference ALL Listed Sub-Items

When the `condition` is a connective that forward-references a list of
sub-items ("both of the following conditions are fulfilled", "any of the
following areas", "where the following applies"), the actual conditions are
the listed sub-items. You MUST include the IRIs of ALL listed sub-items in
`references` — not just the first one.

Worked example: AI Act Art 6(1) reads "an AI system shall be considered to be
high-risk where both of the following conditions are fulfilled: (a) …;
(b) …". The condition is a conjunction of (a) AND (b), so `references` must
include both `aiact:art_6/par_1/pt_a` and `aiact:art_6/par_1/pt_b`. The
list-introducer "both of the following conditions are fulfilled" is itself
inert downstream — what matters are the sub-item IRIs.

Consult the Resolved cross-reference IRIs section in the prompt for the
candidate sub-item IRIs (sibling paragraphs of the current paragraph in the
same article).

{_METHODS_SECTION}

{_COMMON_OUTPUT_FIELDS}

## Instructions

1. Locate the scope clause indicated by the `anchor`.
2. Determine the scope axis using the heuristics above. Lean on the Article
   heading and the article-level position (Article 3 is canonically
   TERRITORIAL).
3. Determine polarity from the phrasing ("applies to" / "applies where" →
   INCLUDES; "does not apply to" / "shall not apply where" → EXCLUDES).
4. Extract applies_to and condition as ExtractedValues with method assigned
   per the priority above. Verify applies_to is a domain/data-type/activity/
   category, not an actor.
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
            # parent.references carries the resolved IRIs of any cross-refs in
            # the ancestor's own text. For exemption chapeaux like "Paragraph N
            # shall not apply", this is the IRI of the rule being exempted —
            # the deontic extractor's exemption-references rule needs it.
            if p.get("references"):
                parts.append(
                    f"    references cited in this lead-in: "
                    f"{', '.join(p['references'])}"
                )

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


def _na_stub(rec: dict, rationale: str, *, anchor: str | None = None,
             error: str | None = None) -> dict:
    """Build a NOT_APPLICABLE ExtractionResult record. Per
    annotation_guide.md §6, `statement.text` carries the anchor (the stage-1
    snippet/paraphrase identifying which part of the paragraph this NA
    covers), NOT the full paragraph text. Multiple NAs per paragraph are
    allowed; each is scoped to its own anchor. Falls back to the paragraph
    text only when the anchor is missing (failure paths like stage1 errors
    and batch timeouts)."""
    text_for_stub = anchor if anchor else rec["text"]
    out = {
        "statement_class": StatementClass.NOT_APPLICABLE.value,
        "statement": {"text": text_for_stub},
        "paragraph_iri": rec["iri"],
        "needs_review": error is not None,
        "classification_rationale": rationale,
    }
    if anchor is not None:
        out["anchor"] = anchor
    if error is not None:
        out["extractor_error"] = error
    return out


def _profile_scan_text(statement_class: str, stmt: dict) -> str:
    """Build the text blob the hc-gate scans for profile-dimension keywords.

    Scoped to the discriminating fields per class — the field that says WHAT
    the rule addresses, plus the immediate qualifier. We deliberately exclude
    the predicate (which carries generic "shall apply" language regardless of
    subject matter) and free-floating fields like classification_rationale.

    - DEONTIC      → subject + object + condition
    - APPLICABILITY → applies_to + condition
    - DEFINITIONAL → term + definition
    """
    parts: list[str] = []

    def _ev(obj):
        if isinstance(obj, dict) and "value" in obj and isinstance(obj.get("value"), str):
            parts.append(obj["value"])
        elif isinstance(obj, list):
            for v in obj:
                _ev(v)
        elif isinstance(obj, str):
            parts.append(obj)

    if statement_class == "DEONTIC":
        _ev(stmt.get("subject"))
        _ev(stmt.get("object"))
        _ev(stmt.get("condition"))
    elif statement_class == "APPLICABILITY":
        _ev(stmt.get("applies_to"))
        _ev(stmt.get("condition"))
    elif statement_class == "DEFINITIONAL":
        if isinstance(stmt.get("term"), str):
            parts.append(stmt["term"])
        _ev(stmt.get("definition"))
    return " ".join(parts).lower()


def _profile_dimensions_matched(statement_class: str, stmt: dict) -> list[str]:
    """Return the MigrainePredict-profile dimensions whose keywords appear in
    the statement's scoped text content."""
    blob = _profile_scan_text(statement_class, stmt)
    return [dim for dim, kws in PROFILE_KEYWORDS.items() if any(kw in blob for kw in kws)]


# Transient-error signatures that warrant a retry rather than falling through
# to the HITL queue. 503 / 429 / UNAVAILABLE / RESOURCE_EXHAUSTED come from
# Gemini under load; the timeout/deadline cases handle network hiccups.
_TRANSIENT_ERROR_MARKERS = (
    "503", "UNAVAILABLE",
    "429", "RESOURCE_EXHAUSTED",
    "timeout", "Timeout", "TIMEOUT",
    "deadline", "Deadline", "DEADLINE",
    "Connection reset", "ECONNRESET",
)
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 2.0
RETRY_BACKOFF_FACTOR = 3.0


def _retry_invoke(chain, payload: dict, *, label: str):
    """Invoke a LangChain chain with exponential backoff on transient errors.
    Delays: 2s, 6s. Non-transient errors raise immediately. After the final
    attempt fails the caller's existing extractor_error path kicks in."""
    last_exc: Exception | None = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            return chain.invoke(payload)
        except Exception as e:
            last_exc = e
            msg = str(e)
            is_transient = any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)
            if not is_transient or attempt == RETRY_MAX_ATTEMPTS - 1:
                raise
            delay = RETRY_BASE_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR ** attempt)
            print(f"  retry {label} (attempt {attempt+1}/{RETRY_MAX_ATTEMPTS}) "
                  f"after {delay:.0f}s — transient error: {msg[:120]}")
            time.sleep(delay)
    raise last_exc  # pragma: no cover — defensive


def _groundedness_flag(statement_class: str, stmt: dict) -> tuple[bool, str | None]:
    """Second HITL trigger, orthogonal to the confidence threshold. Per
    annotation_guide.md §8.2 the gate fires ONLY on CITATION-sourced
    core-identity fields — never on CONTEXT, which is structural (passive-
    voice GDPR routinely puts the duty-bearer in CONTEXT per §3.1).

    Rules:
      - DEONTIC: subject or predicate method is CITATION → flag.
      - DEFINITIONAL: definition method is CITATION → flag.
      - APPLICABILITY: applies_to method is CITATION → flag.

    CITATION catches the case where the LLM reconstructed the core identity
    of the statement from a cross-referenced provision rather than the
    paragraph or its structural context — the legitimate "this whole
    extraction is heavily reconstructed" signal."""
    def methods_of(field):
        v = stmt.get(field)
        if isinstance(v, list):
            return [e.get("method") for e in v if isinstance(e, dict) and e.get("method")]
        if isinstance(v, dict) and v.get("method"):
            return [v["method"]]
        return []

    if statement_class == "DEONTIC":
        for field in ("subject", "predicate"):
            for m in methods_of(field):
                if m == "CITATION":
                    return True, f"{field} method is CITATION (reconstructed from cited provision)"
    elif statement_class == "DEFINITIONAL":
        for m in methods_of("definition"):
            if m == "CITATION":
                return True, "definition method is CITATION (reconstructed from cited provision)"
    elif statement_class == "APPLICABILITY":
        for m in methods_of("applies_to"):
            if m == "CITATION":
                return True, "applies_to method is CITATION (reconstructed from cited provision)"
    return False, None


def _apply_hc_gate(result: dict) -> dict:
    """Rule-based gate on applies_to_healthcare. Three checks override an
    LLM-emitted True to False with needs_review=True:

      1. APPLICABILITY with polarity=EXCLUDES — carve-outs are not in-scope;
         the user is explicit that EXCLUDES-polarity records should not pass
         the gate even if their text keyword-matches.
      2. APPLICABILITY whose `applies_to` is "legal persons" (or otherwise
         clearly non-natural-person scoped) — MigrainePredict's subjects are
         natural persons; legal-persons clauses are inherently irrelevant.
      3. No MigrainePredict profile dimension matches the scoped text.

    NA stubs are exempt. False stays False."""
    stmt = result.get("statement")
    if not stmt or "applies_to_healthcare" not in stmt:
        return result
    if not stmt["applies_to_healthcare"]:
        return result

    cls = result["statement_class"]

    if cls == "APPLICABILITY" and stmt.get("polarity") == "EXCLUDES":
        stmt["applies_to_healthcare"] = False
        result["needs_review"] = True
        result["profile_gate_override"] = (
            "hc=True overridden to False: EXCLUDES-polarity scope is a carve-out, not in-scope"
        )
        return result

    if cls == "APPLICABILITY":
        applies_to_val = (stmt.get("applies_to") or {}).get("value") or ""
        if "legal person" in applies_to_val.lower():
            stmt["applies_to_healthcare"] = False
            result["needs_review"] = True
            result["profile_gate_override"] = (
                "hc=True overridden to False: applies_to targets legal persons, "
                "not MigrainePredict's natural-person subjects"
            )
            return result

    matched = _profile_dimensions_matched(cls, stmt)
    if matched:
        result["profile_dimensions_matched"] = matched
        return result

    stmt["applies_to_healthcare"] = False
    result["needs_review"] = True
    result["profile_gate_override"] = (
        "hc=True overridden to False: no MigrainePredict profile dimension matched "
        "in the scoped text"
    )
    return result


def _process_paragraph(chains, rec: dict) -> tuple[dict, list[dict], bool]:
    """Run the full two-stage pipeline on one paragraph. Returns (rec,
    list-of-result-records, errored). On stage-1 failure or unexpected empty
    classification, emits a NOT_APPLICABLE stub flagged needs_review=True so
    nothing is silently dropped.

    DEONTIC candidates from recital paragraphs are coerced to NOT_APPLICABLE
    as a defense-in-depth rule: recitals are non-binding interpretive guidance
    and should not produce binding obligations even if the classifier slips."""
    cls_chain, deontic_chain, definitional_chain, applicability_chain = chains
    ctx = build_context_bundle(rec)
    iri = rec["iri"]
    is_recital = rec.get("unit_type") == "recital"

    try:
        classification: ParagraphClassification = _retry_invoke(
            cls_chain, {"context_bundle": ctx},
            label=f"stage1 {iri}",
        )
    except Exception as e:
        return rec, [_na_stub(rec, "stage1 failure", error=str(e))], True

    if not classification.candidates:
        return rec, [_na_stub(rec, "stage1 returned empty list", error="empty candidates")], True

    results: list[dict] = []
    errored = False
    # Collapse all NOT_APPLICABLE candidates (and recital-DEONTIC suppressions,
    # which become NA) into a SINGLE NA record per paragraph — guide §6. The
    # classifier sometimes emits one NA candidate per sentence of a wholly
    # non-operative recital; those are merged here so a non-operative paragraph
    # yields exactly one NA node, not a blob of near-identical ones.
    na_rationales: list[str] = []
    na_unknown_error: str | None = None
    for cand in classification.candidates:
        cls = cand.statement_class
        if cls == StatementClass.DEONTIC and is_recital:
            na_rationales.append(
                f"DEONTIC suppressed on recital — {cand.rationale}"
            )
            continue
        if cls == StatementClass.NOT_APPLICABLE:
            na_rationales.append(cand.rationale)
            continue
        if cls == StatementClass.DEONTIC:
            stage2 = deontic_chain
        elif cls == StatementClass.DEFINITIONAL:
            stage2 = definitional_chain
        elif cls == StatementClass.APPLICABILITY:
            stage2 = applicability_chain
        else:
            na_rationales.append(f"unknown class {cls}")
            na_unknown_error = f"unknown class {cls}"
            errored = True
            continue

        try:
            stmt = _retry_invoke(
                stage2, {"context_bundle": ctx, "anchor": cand.anchor},
                label=f"stage2 {cls.value} {iri}",
            )
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
        # mode='json' serialises enum fields to their string values (e.g.
        # 'CONTEXT' rather than ExtractionMethod.CONTEXT) so downstream string
        # comparisons and audit messages get clean values.
        stmt_dict = stmt.model_dump(mode="json")
        if stmt_dict.get("source_article") != iri:
            stmt_dict["source_article"] = iri

        confidence_low = stmt.confidence < HITL_THRESHOLD
        low_grounding, ground_reason = _groundedness_flag(cls.value, stmt_dict)
        result = {
            "statement_class": cls.value,
            "statement": stmt_dict,
            "paragraph_iri": iri,
            "needs_review": confidence_low or low_grounding,
            "classification_rationale": cand.rationale,
            "anchor": cand.anchor,
        }
        if low_grounding:
            result["low_groundedness"] = ground_reason
        results.append(_apply_hc_gate(result))

    # Emit exactly one merged NA record if any NA candidates were collected.
    # statement.text is the full paragraph text (one record, so no blobbing);
    # rationales from the merged candidates are joined for the audit trail.
    if na_rationales:
        seen = set()
        unique_rationales = [r for r in na_rationales
                             if not (r in seen or seen.add(r))]
        merged = _na_stub(
            rec,
            " | ".join(unique_rationales),
            error=na_unknown_error,
        )
        results.append(merged)

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
                        [_na_stub(rec, "batch deadline exceeded",
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
