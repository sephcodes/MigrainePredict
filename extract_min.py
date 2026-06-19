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
import re
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

# MigrainePredict-profile keyword sets for the rule-based applies_to_healthcare
# gate. A statement whose scoped text matches no dimension has an LLM-emitted
# hc=True overridden to False with needs_review=True. Matched substring-wise on
# lowercased text.
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

# MigrainePredict's OPERATIVE basis for special-category processing — the only
# Art 9(2) derogations MP actually relies on (consent + medical). Sourced from
# MP's stated compliance posture, NOT synthesised here. Used by the gate's
# operative-basis layer: an Art 9(2) derogation OUTSIDE this set touches the
# special-category keyword but is not MP-relevant (e.g. research 9(2)(j)), so
# hc is set False and the record is flagged for review. MP's high-risk basis is
# the AI Act Art 6(1) medical-device route, so Annex III high-risk USE-areas
# (standalone biometrics etc.) are likewise not MP's operative basis.
MIGRAINEPREDICT_PROFILE = {
    "special_category_bases": {"gdpr:art_9/par_2/pt_a", "gdpr:art_9/par_2/pt_h"}, # explicit consent and preventive/occupational medicine, health care
    "excluded_scope_prefixes": ("aiact:anx_III/par_",)
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
    references: list[str] = Field(
        default_factory=list,
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
    references: list[str] = Field(
        default_factory=list,
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
    references: list[str] = Field(
        default_factory=list,
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
- **applies_to_healthcare**: true iff MigrainePredict's controller must
  satisfy / reason with this provision when operating the system.
  MigrainePredict processes biometric/health data of identifiable natural
  persons and is a high-risk AI system on the medical-device pathway.
  TRUE for: foundational definitions (personal data, processing, identifiable
  natural person, controller, processor, biometric data, health data); general
  processing principles (Art 5); lawful basis (Art 6, Art 9); data-subject
  rights (Arts 12-22); controller obligations; AI Act high-risk / provider /
  deployer rules. FALSE for: provisions specific to other sectors (employment,
  journalism, research, public authorities, religious associations), internal
  regulatory machinery, and final/commencement provisions."""


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
  exhortations as NOT_APPLICABLE (or DEFINITIONAL / APPLICABILITY if they
  supply definitional criteria or a scope test that mirrors an operative
  Article).
- **DEFINITIONAL** — defines a regulatory term (e.g., GDPR Art 4 definitions,
  AI Act Art 3 definitions, or a recital supplying definitional criteria such
  as identifiability tests). The paragraph must contain ACTUAL intensional
  definitional content. Two patterns are NOT DEFINITIONAL:
    - **Pointer-only**: the only content is a reference to an external source
      ("the notion of X should draw from Article 2 of Recommendation Y") →
      NOT_APPLICABLE.
    - **Forward-reference / empty connective**: the term is "defined" by a
      bare list-introducer pointing elsewhere ("X shall be considered to be
      high-risk where the following conditions are fulfilled", "X are the AI
      systems listed in the following areas") → APPLICABILITY (scope_type =
      MATERIAL, polarity = INCLUDES, condition expanded from the connective).
- **APPLICABILITY** — a material, territorial, personal, or temporal scope
  clause. Phrasings: "this Regulation applies to…", "this Regulation does not
  apply where…", "shall apply only to…".
- **NOT_APPLICABLE** — the paragraph contains none of the above (preamble,
  internal regulatory machinery, pointer-only references to external
  definitions, bare cross-reference, commencement language, recital "should"
  interpretive guidance, or otherwise carries nothing extractable).

## Exemption Clauses Are DEONTIC, Not APPLICABILITY

Clauses of the form "X shall not apply where Y" attached to a parent rule are
operative exceptions, NOT freestanding scope clauses. Classify them DEONTIC
(stage 2 sets the modality by inverting the parent rule). Only true overall
scope clauses (Art 2, Art 3) are APPLICABILITY.

## Instructions

1. Read the paragraph together with its parent chain (lead-in clauses above)
   and its sibling paragraphs. Note the `Source:` line — if `unit_type` is
   `recital`, DEONTIC is forbidden.
2. Identify each distinct statement the paragraph carries.
3. For each statement, assign exactly one class.
4. Write a short `anchor` (≤20 words) — a verbatim snippet from the paragraph
   (preferred) or close paraphrase — identifying which part of the paragraph
   the candidate refers to. Required for ALL classes including NOT_APPLICABLE;
   do not use a placeholder like '—'.
5. Write a one-sentence `rationale` (≤25 words) describing CLASS membership
   only. Do NOT name a specific modality ("obligation", "permission",
   "prohibition", "dispensation") — modality is decided at stage 2.
6. If the paragraph carries multiple statements, emit one candidate per
   statement.
7. If the paragraph carries nothing extractable, emit a single candidate with
   class NOT_APPLICABLE.

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

## Data-Subject Rights → One Candidate (the controller's duty)

This rule applies ONLY to the explicit data-subject RIGHTS in Articles 12–22,
phrased "the data subject shall have the right to / not to X". It does NOT
apply to lawful-basis or consent provisions (e.g. Art 6(1), Art 9(2)) — those
mention the data subject's consent but are ordinary controller PERMISSIONs;
classify and extract them normally, and never drop or merge them.

For an actual Art 12–22 right: it is ONE deontic statement, extracted at stage
2 as the correlative CONTROLLER duty. Emit EXACTLY ONE DEONTIC candidate for
it, anchored on the right. Do NOT additionally emit a separate "the data
subject may / has the right" candidate — the right and the controller's duty
are the same statement modelled once, not two.

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

## Data-Subject Rights Are Extracted as the Controller's Duty

This applies ONLY to the explicit Article 12–22 rights ("the data subject
shall have the right to / not to X"), NOT to lawful-basis or consent
provisions (Art 6(1), Art 9(2)) — extract those normally as controller
PERMISSIONs. For an actual Art 12–22 right, extract it as the correlative duty
of the CONTROLLER — never as a statement held by the data subject.

- **subject** = "the controller" (method CONTEXT; the controller is the
  implied duty-bearer, not named in the right's wording).
- **beneficiary** = "the data subject".
- **modality** follows what the controller must DO:
    - controller must PROVIDE / CARRY OUT / ENABLE something (access,
      rectification, erasure, portability, restriction) → **OBLIGATION**.
    - controller must REFRAIN FROM / CEASE something (Art 22 solely-automated
      decisions; ceasing processing after an Art 21 objection) → **PROHIBITION**.
- **predicate / object** describe the controller's action on the data.

Do NOT output a statement whose subject is "the data subject" — the data
subject is the beneficiary, never the duty-bearer. (Exceptions to these duties
— e.g. Art 17(3) carve-outs to the erasure obligation — follow the
exemption-modality rule below: an exception to an OBLIGATION → DISPENSATION.)

## Modality of Exemptions — Invert the Parent Rule

When the statement is an exception attached to a parent rule ("X shall not
apply where Y", "by way of derogation from paragraph N"), its modality is the
INVERSE of the parent rule's modality. Do NOT default every exception to
DISPENSATION — read the parent first, then flip:

- Parent is a PROHIBITION ("shall be prohibited", "shall not") → exception is
  **PERMISSION** (the carve-out ALLOWS what was prohibited). Example: Art
  9(2)(h) is an exception to the Art 9(1) special-category PROHIBITION →
  modality = PERMISSION.
- Parent is an OBLIGATION ("shall", "must") → exception is **DISPENSATION**
  (the carve-out RELEASES the addressee from the duty).

For a PERMISSION/DISPENSATION carve-out the `subject` is the party receiving
the permission (the controller), not a safeguard actor named in the carve-out
(e.g. "a professional subject to professional secrecy" is a CONDITION, not the
subject).

## Exemption References — Use the Parent Chain's `references cited`

A PERMISSION/DISPENSATION carve-out MUST include the parent rule's IRI in
`references`. The parent chain in the prompt lists ancestors as `[<iri>]
<text>` lines; an exemption chapeau ("Paragraph 1 shall not apply") has its
resolved IRIs on a "references cited in this lead-in:" line directly beneath
it. Copy those into `references`, plus any safeguards / condition-source IRIs
from the statement's own cross-references.

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
sub-clause introduced by "which", "that", "such as", "including" is INSIDE the
definition. Do NOT truncate at internal commas or relative-pronoun boundaries
— the legally load-bearing content of EU definitions is routinely in the
SECOND HALF of the clause:

- AI Act Art 3(1) "AI system": the full clause runs through "…that can
  influence physical or virtual environments". The "infers … how to generate
  outputs" segment is the operative discriminator. Cutting at "after
  deployment" gives the wrong definition.
- GDPR Art 4(14) "biometric data": the full clause runs through "…such as
  facial images or dactyloscopic data". The "which allow or confirm the
  unique identification" segment is the operative discriminator.

**Stopping rule**: the definition ends at the semicolon that closes the term's
entry. Internal commas and relative clauses are inside it.

When a paragraph packs multiple definitions separated by end-of-definition
semicolons (e.g. GDPR Art 4(1) defines both "personal data" AND "identifiable
natural person"), emit one DEFINITIONAL candidate per term — each carrying the
full clause for its own term.

{_METHODS_SECTION}

{_COMMON_OUTPUT_FIELDS}

## Instructions

1. Locate the definition indicated by the `anchor`.
2. Set `term` to the defined term as written in the regulation.
3. Set `definition.value` to the COMPLETE definitional clause (per the
   completeness rule above) and assign its method. If it is the paragraph's
   own text, method is STATED.
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
  product class, or category — NOT an actor or role. If it comes out as an
  actor ("Member States", "controllers", "the Commission"), re-examine: the
  actor is the *agent* of the scope rule; the *thing scoped* is the rules,
  the processing, or the category being qualified. Keep applies_to to the
  scoped thing only — do not append the condition text to it.
- **condition**: the condition that triggers applicability or exclusion.
- **polarity**: INCLUDES (brings within scope) or EXCLUDES (places outside
  scope).

## Scope-Type Heuristics

- **TERRITORIAL** — trigger is establishment, residence, location, geography
  ("established in the Union", "regardless of whether the processing takes
  place in the Union"). Article 3 of GDPR is canonically TERRITORIAL; an
  incidental "natural persons" does not make it PERSONAL.
- **MATERIAL** — trigger is a data type or processing activity ("processing
  of personal data", "placing on the market of high-risk AI systems").
- **PERSONAL** — trigger is the natural-vs-legal-person distinction or a class
  of natural persons ("natural persons", "children", "legal persons").
- **TEMPORAL** — trigger is a date / transition period.

## applies_to Completeness and Boundary

`applies_to` is the COMPLETE noun phrase naming the scoped entity. Capture all
of it, including any coordinated continuation that names MORE of the same
entity — "X, as well as Y", "X and Y", "X or Y" where Y is another thing being
scoped. Do NOT stop at an internal comma and drop the coordinated tail (e.g.
from "documents, as well as their cover pages" do not keep only "documents").

`condition` is the separate material that QUALIFIES or RESTRICTS the scope —
not part of the entity name. It includes manner / means phrases ("by automated
means", "through ..."), conditionals ("if ...", "where ...", "provided that
..."), and relative clauses ("which ...", "that ..."). These belong in
`condition`, never appended to `applies_to`.

Split test: text that NAMES the scoped thing → `applies_to` (including
coordinated names); text that says WHEN / HOW / WHICH it applies → `condition`.
A phrase joined by "as well as" / "and" follows the same test — it joins
`applies_to` only if it names another entity, and `condition` if it describes
a manner, means, or restriction.

## Manner / means are never part of applies_to

A phrase describing HOW or BY WHAT MEANS the activity is performed —
"by automated means", "to manual processing", "through X", "using Y" — is a
manner phrase and ALWAYS goes in condition, even when introduced by "as well
as" or "and". The "as well as Y" completeness rule applies only when Y NAMES a
further entity (a noun the regulation scopes, e.g. "cover pages"), not when Y
describes a means of acting on the entity already named.

Test: can Y stand alone as a thing the regulation scopes (a noun)? -> applies_to.
Does Y answer "how / by what means"? -> condition.

Example: "processing of personal data by automated means, as well as to manual
processing" -> applies_to = "processing of personal data";
condition = "by automated means, as well as to manual processing, if ...".

## List-Introducer Conditions — Reference ALL Listed Sub-Items

When the condition is a connective forward-referencing a list ("both of the
following conditions are fulfilled", "any of the following areas"), the actual
conditions are the listed sub-items. Include the IRIs of ALL of them in
`references` (consult the Resolved cross-reference IRIs in the prompt), not
just the first.

{_METHODS_SECTION}

{_COMMON_OUTPUT_FIELDS}

## Instructions

1. Locate the scope clause indicated by the `anchor`.
2. Determine the scope axis using the heuristics above; lean on the Article
   heading (Article 3 is canonically TERRITORIAL).
3. Determine polarity from the phrasing ("applies to" / "applies where" →
   INCLUDES; "does not apply to" / "shall not apply where" → EXCLUDES).
4. Extract applies_to (the scoped thing only, not an actor, not the condition)
   and condition as ExtractedValues with method assigned per the priority.
5. Set source_article from the prompt and pick the references subset (all
   listed sub-items for a list-introducer condition).
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
            # Resolved cross-refs found in the ancestor's own text. For an
            # exemption chapeau ("Paragraph N shall not apply"), this is the
            # IRI of the rule being exempted — the deontic exemption-references
            # rule needs it.
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


# Transient-error signatures that warrant a retry rather than falling through
# to the HITL queue (Gemini under load / network hiccups).
_TRANSIENT_ERROR_MARKERS = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
    "timeout", "Timeout", "TIMEOUT", "deadline", "Deadline", "DEADLINE",
    "Connection reset", "ECONNRESET",
)
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 2.0
RETRY_BACKOFF_FACTOR = 3.0


def _retry_invoke(chain, payload: dict, *, label: str):
    """Invoke a chain with exponential backoff (2s, 6s) on transient errors.
    Non-transient errors raise immediately; after the final attempt the
    caller's extractor_error path takes over."""
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
    raise last_exc  # pragma: no cover


def _profile_scan_text(statement_class: str, stmt: dict) -> str:
    """Scoped text the hc-gate scans, per class. Excludes predicate (generic
    'shall apply' language) and free-floating provenance fields."""
    parts: list[str] = []

    def _ev(obj):
        if isinstance(obj, dict) and isinstance(obj.get("value"), str):
            parts.append(obj["value"])
        elif isinstance(obj, list):
            for v in obj:
                _ev(v)
        elif isinstance(obj, str):
            parts.append(obj)

    if statement_class == "DEONTIC":
        _ev(stmt.get("subject")); _ev(stmt.get("object")); _ev(stmt.get("condition"))
    elif statement_class == "APPLICABILITY":
        _ev(stmt.get("applies_to")); _ev(stmt.get("condition"))
    elif statement_class == "DEFINITIONAL":
        if isinstance(stmt.get("term"), str):
            parts.append(stmt["term"])
        _ev(stmt.get("definition"))
    return " ".join(parts).lower()


def _profile_dimensions_matched(statement_class: str, stmt: dict) -> list[str]:
    blob = _profile_scan_text(statement_class, stmt)
    return [dim for dim, kws in PROFILE_KEYWORDS.items() if any(kw in blob for kw in kws)]


def _is_legislator_subject(stmt: dict) -> bool:
    """True when a DEONTIC statement's subject is a legal INSTRUMENT
    ("Union or Member State law", "Member State law", "national law") rather
    than a duty-bearing actor. These arise when a derogation's safeguards tail
    ("based on Union or Member State law which shall be proportionate…") is
    mis-extracted as standalone obligations addressed to the law itself — the
    content is already captured in the parent permission's `condition`, so the
    record is a spurious duplicate.

    Matches only subjects whose head noun is "law"; "Member States" (a genuine
    duty-bearer in our subject convention) is NOT matched. Requires EVERY
    subject value to be a legal instrument, so a real actor anywhere in the
    subject list keeps the record."""
    subs = stmt.get("subject") or []
    vals = [(sv.get("value") or "").strip().lower()
            for sv in subs if isinstance(sv, dict)]
    vals = [v for v in vals if v]
    if not vals:
        return False
    return all(v.endswith(" law") or v == "law" for v in vals)


def _apply_hc_gate(result, profile=MIGRAINEPREDICT_PROFILE) -> dict:
    """Rule-based gate on applies_to_healthcare.

    Operative-basis layer (fires regardless of the LLM's hc value, because a
    special-category/biometric provision must not be ASSERTED hc=True on
    keyword presence alone):
      0a. Art 9(2) derogation outside MP's operative basis (not 9(2)(a)/(h))
          → hc=False + needs_review=True.
      0b. Annex III high-risk USE-area (anx_III/par_N, not the chapeau p_0)
          → hc=False + needs_review=True (MP is high-risk via Art 6(1)
          medical-device, not via an Annex III area).

    Keyword layer (only when the LLM emitted hc=True):
      1. APPLICABILITY EXCLUDES polarity → False (carve-out, not in-scope).
      2. APPLICABILITY applies_to = legal persons → False.
      3. No profile dimension matched → False.
    Each override sets needs_review=True + an audit field. False stays False."""
    stmt = result.get("statement")
    if not stmt or "applies_to_healthcare" not in stmt:
        return result

    src = stmt.get("source_article") or ""

    # 0a. Special-category derogation outside MP's operative basis.
    if src.startswith("gdpr:art_9/par_2/pt_") and src not in profile["special_category_bases"]:
        stmt["applies_to_healthcare"] = False
        result["needs_review"] = True
        result["profile_gate_override"] = (
            "hc=False: Art 9(2) derogation outside the system's operative "
            "special-category basis — keyword present, not the basis; flagged for review"
        )
        return result

    # 0b. Annex III high-risk USE-area (not the classification chapeau p_0).
    if any(src.startswith(p) for p in profile["excluded_scope_prefixes"]):
        stmt["applies_to_healthcare"] = False
        result["needs_review"] = True
        result["profile_gate_override"] = (
            "hc=False: scope outside the system's operative high-risk basis "
            "— keyword present, not the basis; flagged for review"
        )
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


def _na_stub(rec: dict, rationale: str, *, error: str | None = None) -> dict:
    """Build a NOT_APPLICABLE record. One NA per paragraph, so statement.text
    is the full paragraph text (no duplication)."""
    out = {
        "statement_class": StatementClass.NOT_APPLICABLE.value,
        "statement": {"text": rec["text"]},
        "paragraph_iri": rec["iri"],
        "needs_review": error is not None,
        "classification_rationale": rationale,
    }
    if error is not None:
        out["extractor_error"] = error
    return out


# Canonical duty-bearer vocabulary (plain roles, matching the gold). The LLM
# picks WHICH role; this pass only snaps the surface form to one canonical
# string and fixes the method, so the 'who-must-act' field is stable run-to-run
# for Phase-2 queries. Ontology-IRI mapping (dpv:DataController etc.) is a
# later stage. Order: more specific keywords first.
_ROLE_KEYWORDS = [
    ("data subject", "the data subject"),
    ("supervisory authorit", "supervisory authorities"),
    ("member state", "Member States"),
    ("controller", "the controller"),
    ("processor", "the processor"),
    ("provider", "the provider"),
    ("deployer", "the deployer"),
]
# Core data-processing actors that are regulation-specific (used for the
# vocabulary mismatch check). Member States / authorities are cross-cutting and
# never flag.
_GDPR_CORE_ROLES = {"the controller", "the processor", "the data subject"}
_AIACT_CORE_ROLES = {"the provider", "the deployer"}
_DEFAULT_DUTY_BEARER = {"gdpr": "the controller", "aiact": "the provider"}


# Value-type markers for routing a dropped PERMISSION grammatical subject:
# a data/patient noun is the OBJECT ("special categories ... may be processed"),
# a scope entity is the exemption CONDITION ("[obligations] shall not apply to
# an enterprise ...").
_DATA_NOUN_MARKERS = ("data", "information", "categor", "decision", "record", "content")
_SCOPE_ENTITY_MARKERS = ("enterprise", "organisation", "organization", "body",
                         "establishment", "established", "employing", "undertaking",
                         "micro", "medium-sized")


def _preserve_target_slot(modality: str, value: str) -> str | None:
    """Which slot a dropped grammatical-subject value belongs in, given the
    modality. DISPENSATION -> condition (exemption scope). OBLIGATION/PROHIBITION
    -> object (passive patient). PERMISSION is ambiguous so it is routed by
    value-type: scope entity -> condition, data/patient noun -> object,
    otherwise not moved (we don't guess)."""
    if modality == "DISPENSATION":
        return "condition"
    if modality in ("OBLIGATION", "PROHIBITION"):
        return "object"
    if modality == "PERMISSION":
        v = (value or "").lower()
        if any(m in v for m in _SCOPE_ENTITY_MARKERS):
            return "condition"
        if any(m in v for m in _DATA_NOUN_MARKERS):
            return "object"
    return None


def _preserve_dropped_subject(st: dict, modality: str, dropped: list[str]) -> None:
    """Re-home a dropped non-role grammatical subject into object or condition
    (per _preserve_target_slot) so compliance-relevant content isn't lost when
    the duty-bearer substitution fires. De-dups against the target slot; for a
    condition, prepends the scope and keeps the existing tail."""
    def _present(needle: str, hay: str) -> bool:
        n, h = (needle or "").lower().strip(), (hay or "").lower()
        return bool(n) and (n in h or h in n)

    for val in dropped:
        if not val:
            continue
        slot = _preserve_target_slot(modality, val)
        if slot == "object":
            objs = st.get("object") or []
            if not any(_present(val, ev.get("value")) for ev in objs):
                objs.append({"value": val, "method": "STATED"})
                st["object"] = objs
        elif slot == "condition":
            cond = st.get("condition")
            existing = cond.get("value") if isinstance(cond, dict) else None
            if not existing:
                st["condition"] = {"value": val, "method": "STATED"}
            elif not _present(val, existing):
                st["condition"] = {"value": f"{val} {existing}", "method": "STATED"}
        # slot is None -> ambiguous PERMISSION value; leave it out rather than guess.


def _canonical_role(value: str) -> tuple[str | None, str | None]:
    """Map a subject surface form to (canonical_role, matched_keyword), or
    (None, None) when it doesn't map to exactly one role (multi-role or
    unknown values are left untouched so we never corrupt them)."""
    v = (value or "").lower()
    hits = [(canon, kw) for kw, canon in _ROLE_KEYWORDS if kw in v]
    # de-dupe by canonical form (e.g. one keyword) — distinct canon forms means
    # the value names more than one role; leave it alone.
    distinct = {c for c, _ in hits}
    if len(distinct) == 1:
        return hits[0]
    return None, None


def _canonicalize_subjects(rec: dict, results: list[dict]) -> None:
    """Deterministic subject-canonicalization (Item 1). For each DEONTIC
    statement: snap subject values to the canonical role vocabulary; recompute
    method as STATED iff the role word is named in the paragraph (else CONTEXT);
    supply the regulation's default duty-bearer when the subject is implicit
    (passive obligation-of-being); and flag a wrong-regulation actor for review.
    Subject is interpretive (soft-graded), so this only stabilises the field and
    cannot change a HARD outcome."""
    source = rec.get("source", "")
    text_lc = (rec.get("text") or "").lower()
    default = _DEFAULT_DUTY_BEARER.get(source)

    for r in results:
        if r["statement_class"] != "DEONTIC":
            continue
        st = r.get("statement")
        if not st:
            continue

        subj = st.get("subject") or []
        for ev in subj:
            canon, _ = _canonical_role(ev.get("value"))
            if canon:
                ev["value"] = canon  # value-only canonicalisation; method left as the LLM's

        # Passive obligation-of-being / non-actor subject: when NO subject names
        # a duty-bearer role at all (empty, or a passive grammatical subject
        # such as "personal data" or "enterprise employing fewer than 250
        # persons"), the implied duty-bearer is the regulation default (CONTEXT).
        # A subject containing any role word — including multi-role "controllers
        # and processors" — is left as-is. Flagged for audit.
        def _has_role_word(ev: dict) -> bool:
            v = (ev.get("value") or "").lower()
            return any(kw in v for kw, _ in _ROLE_KEYWORDS)
        if default and not any(_has_role_word(ev) for ev in subj):
            dropped = [ev.get("value") for ev in subj if ev.get("value")]
            st["subject"] = [{"value": default, "method": "CONTEXT"}]
            r["subject_inferred_duty_bearer"] = True
            _preserve_dropped_subject(st, st.get("modality"), dropped)

        # Regulation-vocabulary check (core actors only).
        vals = {ev.get("value") for ev in (st.get("subject") or []) if ev.get("value")}
        wrong = None
        if source == "gdpr" and (vals & _AIACT_CORE_ROLES):
            wrong = vals & _AIACT_CORE_ROLES
        elif source == "aiact" and (vals & _GDPR_CORE_ROLES):
            wrong = vals & _GDPR_CORE_ROLES
        if wrong:
            r["needs_review"] = True
            r["subject_vocabulary_mismatch"] = (
                f"{source} statement with wrong-regulation actor(s) {sorted(wrong)}"
            )


def _assign_statement_ids(iri: str, results: list[dict]) -> None:
    """Give every emitted record a stable, unique `statement_id` of the form
    '<paragraph_iri>#s<N>' — the node identity each statement needs for the KG
    and for intra-paragraph references (Phase B). N is assigned by a CANONICAL
    SORT of the paragraph's non-NA statements (class → discriminator →
    normalized content), so the id is deterministic within a run regardless of
    the LLM's emission order. Output list order is left untouched (emission
    order); only the id reflects canonical position. The single NA stub, if
    any, gets '#na'."""
    def sort_key(r: dict):
        sc = r["statement_class"]
        st = r.get("statement") or {}
        if sc == "DEONTIC":
            preds = " ".join((ev.get("value") or "") for ev in (st.get("predicate") or [])
                             if isinstance(ev, dict))
            objs = " ".join((ev.get("value") or "") for ev in (st.get("object") or [])
                            if isinstance(ev, dict))
            return (sc, st.get("modality") or "", preds.lower(), objs.lower())
        if sc == "DEFINITIONAL":
            return (sc, (st.get("term") or "").lower(), "", "")
        if sc == "APPLICABILITY":
            at = (st.get("applies_to") or {}).get("value") or ""
            return (sc, st.get("scope_type") or "", st.get("polarity") or "", at.lower())
        return (sc, "", "", "")

    statements = sorted((r for r in results if r["statement_class"] != "NOT_APPLICABLE"),
                        key=sort_key)
    for n, r in enumerate(statements, 1):
        r["statement_id"] = f"{iri}#s{n}"
    for r in results:
        if r["statement_class"] == "NOT_APPLICABLE":
            r["statement_id"] = f"{iri}#na"


def _link_intra_paragraph_parents(results: list[dict]) -> None:
    """Phase B2: link each PERMISSION/DISPENSATION carve-out to the parent rule
    it excepts WITHIN the same paragraph, by adding the sibling's statement_id
    to its `references`. A PERMISSION excepts a PROHIBITION; a DISPENSATION
    excepts an OBLIGATION (the same modality correlation the exemption-modality
    rule uses). Only intra-paragraph parents are handled here — a cross-paragraph
    parent is already added by the prompt's exemption-references rule, and the
    modality correlation means a permission with no PROHIBITION sibling (e.g.
    Art 12(1)'s 'may provide orally') is never falsely linked. The added link
    carries `intra_paragraph_inferred` so it is distinguishable from
    LLM-emitted references and reviewable. Must run AFTER statement ids."""
    correlate = {"PERMISSION": "PROHIBITION", "DISPENSATION": "OBLIGATION"}

    def content_tokens(r: dict) -> set:
        st = r.get("statement") or {}
        parts = []
        for fld in ("object", "predicate"):
            for ev in (st.get(fld) or []):
                if isinstance(ev, dict):
                    parts.append(ev.get("value") or "")
        c = st.get("condition")
        if isinstance(c, dict):
            parts.append(c.get("value") or "")
        return set(" ".join(parts).lower().split())

    for r in results:
        st = r.get("statement") or {}
        parent_mod = correlate.get(st.get("modality"))
        if not parent_mod:
            continue
        candidates = [c for c in results if c is not r
                      and (c.get("statement") or {}).get("modality") == parent_mod]
        if not candidates:
            continue  # parent is cross-paragraph (handled by exemption-references)
        if len(candidates) == 1:
            parent = candidates[0]
        else:
            rt = content_tokens(r)
            parent = max(candidates, key=lambda c: len(rt & content_tokens(c)))
        pid = parent.get("statement_id")
        if not pid:
            continue
        refs = st.get("references")
        if not isinstance(refs, list):
            refs = []
            st["references"] = refs
        if pid not in refs:
            refs.append(pid)
            r["intra_paragraph_inferred"] = True


def _process_paragraph(chains, rec: dict) -> tuple[dict, list[dict], bool]:
    """Run the full two-stage pipeline on one paragraph. Returns (rec,
    list-of-result-records, errored). DEONTIC candidates on recitals are
    coerced to NA; all NA candidates collapse to a single NA record per
    paragraph. Stage-1 failure / empty classification emits a flagged NA stub
    so nothing is silently dropped."""
    cls_chain, deontic_chain, definitional_chain, applicability_chain = chains
    ctx = build_context_bundle(rec)
    iri = rec["iri"]
    is_recital = rec.get("unit_type") == "recital"

    try:
        classification: ParagraphClassification = _retry_invoke(
            cls_chain, {"context_bundle": ctx}, label=f"stage1 {iri}")
    except Exception as e:
        return rec, [_na_stub(rec, "stage1 failure", error=str(e))], True

    if not classification.candidates:
        return rec, [_na_stub(rec, "stage1 returned empty list", error="empty candidates")], True

    results: list[dict] = []
    errored = False
    na_rationales: list[str] = []          # collected, merged into one NA at the end
    na_unknown_error: str | None = None
    n_legislator_dropped = 0               # spurious legal-instrument-subject deontics
    for cand in classification.candidates:
        cls = cand.statement_class
        if cls == StatementClass.DEONTIC and is_recital:
            na_rationales.append(f"DEONTIC suppressed on recital — {cand.rationale}")
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
                label=f"stage2 {cls.value} {iri}")
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

        # mode='json' serialises enums to their string values; override
        # source_article with the authoritative input IRI if it drifts.
        stmt_dict = stmt.model_dump(mode="json")
        if stmt_dict.get("source_article") != iri:
            stmt_dict["source_article"] = iri

        # Legislator-subject guard: drop deontic statements whose subject is a
        # legal instrument (not a duty-bearer). These are derogation safeguards
        # tails mis-read as obligations addressed to "Union or Member State
        # law"; the content already lives in the parent permission's condition.
        if cls == StatementClass.DEONTIC and _is_legislator_subject(stmt_dict):
            n_legislator_dropped += 1
            continue

        result = {
            "statement_class": cls.value,
            "statement": stmt_dict,
            "paragraph_iri": iri,
            "needs_review": stmt.confidence < HITL_THRESHOLD,
            "classification_rationale": cand.rationale,
            "anchor": cand.anchor,
        }
        results.append(_apply_hc_gate(result))

    # Collapse all NA candidates into exactly one NA record for the paragraph.
    if na_rationales:
        seen = set()
        unique = [r for r in na_rationales if not (r in seen or seen.add(r))]
        results.append(_na_stub(rec, " | ".join(unique), error=na_unknown_error))

    if n_legislator_dropped:
        print(f"  dropped {n_legislator_dropped} legislator-subject deontic record(s) at {iri}")

    _canonicalize_subjects(rec, results)
    _assign_statement_ids(iri, results)
    _link_intra_paragraph_parents(results)
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
