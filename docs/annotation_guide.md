# Annotation Guide — Deontic / Definitional / Applicability Extraction

This is the **single source of truth** for extraction conventions used by
`extract.py` to parse post-screened paragraphs from the GDPR and EU AI Act
into structured statements. The CoI prompts in `extract.py` mirror this
document; when a rule changes, edit this file AND the corresponding prompt
section. The prompt patches are NOT the rule of record.

## 1. Statement Classes

Stage 1 classifies each candidate statement into exactly one of:

| Class | Definition |
| --- | --- |
| **DEONTIC** | An obligation, permission, prohibition, or dispensation addressed to a regulatory subject. |
| **DEFINITIONAL** | Defines a regulatory term (or supplies definitional criteria for one). |
| **APPLICABILITY** | A MATERIAL, TERRITORIAL, PERSONAL, or TEMPORAL scope clause. |
| **NOT_APPLICABLE** | Carries no extractable statement (preamble, internal machinery, pointer-only definitions, commencement language). |

### 1.1 Class-level rules

- **Recital DEONTIC suppression.** Recitals are non-binding interpretive
  guidance. Any classifier output of DEONTIC on a `unit_type == "recital"`
  paragraph is coerced to NOT_APPLICABLE by orchestration. Recitals can still
  emit DEFINITIONAL or APPLICABILITY.

- **Pointer-only DEFINITIONAL.** A sentence whose only content is a pointer
  to an external source ("the notion of X should draw from Article 2 of
  Recommendation Y") is NOT_APPLICABLE, not DEFINITIONAL.

- **Forward-reference DEFINITIONAL → APPLICABILITY.** When the "definition"
  body is an empty connective ("where the following conditions are
  fulfilled", "as listed in", "as defined in Article X"), reclassify as
  APPLICABILITY MATERIAL with the connective expanded into the condition.
  Example: AI Act Art 6(1) defines high-risk via a forward reference to
  points (a) and (b) — it is APPLICABILITY MATERIAL, not DEFINITIONAL.

- **Exemption clauses are DEONTIC, not APPLICABILITY.** "X shall not apply
  where Y" attached to a parent rule is an operative exception. Classify by
  the parent rule:
  - Exception to a PROHIBITION → DEONTIC with modality **PERMISSION**.
  - Exception to an OBLIGATION → DEONTIC with modality **DISPENSATION**.

  Only true scope clauses (Art 2, Art 3 — defining the regulation's overall
  reach) are APPLICABILITY.

- **Classifier rationale must not pre-commit to a specific modality.** The
  stage-1 rationale describes class membership ("DEONTIC exception",
  "DEONTIC processing principle", "DEFINITIONAL of a regulatory term"), NOT
  the specific modality. Modality is determined at stage 2 from the modal
  verb and (for exemptions) from inverting the parent rule per §3.5. A
  stage-1 rationale that says "DEONTIC dispensation" pre-empts the flip and
  will contradict the stage-2 modality field if the parent is a PROHIBITION
  (then modality = PERMISSION). Use class-level language only.

## 2. Element-Extraction Methods

Every `ExtractedValue` carries a `method` field. Assign exactly one method
per element, in priority order:

1. **STATED** — element appears verbatim or as a close paraphrase in the
   paragraph text itself.
2. **CONTEXT** — element is drawn from the parent chain or a sibling
   paragraph of the same unit (article / annex / recital).
3. **CITATION** — element is drawn from a cross-referenced provision whose
   text is bundled in the prompt.
4. **NONE** — element is absent or unsupported; `value` must be null.

### 2.1 No inference from silence

Galli's BACKGROUND_KNOWLEDGE method is **removed**. If the bundled context
(paragraph + parent + siblings + cited provisions) does not support an
element, set method to NONE and value to null. Never fill values from general
knowledge.

## 3. DEONTIC Schema and Conventions

### 3.1 `subject` and `predicate` — **Active-voice duty-bearer** *(frozen)*

DEONTIC statements are normalised to **active voice throughout**. Subject and
predicate compose: `<subject> <predicate>` must read as a coherent
active-voice clause ("the controller shall process …", not "the controller
shall be processed").

**`subject`** is always the regulatory actor the modality binds — the
duty-bearer role. Allowed values are roles like "the controller", "the
processor", "providers", "deployers", "Member States", "supervisory
authorities", "data subjects". NEVER the grammatical subject of a passive
sentence ("Personal data shall be processed" → subject is the controller,
not "Personal data").

**`predicate`** is the deontic action verb phrase **transformed into the
active voice** when the regulation's surface form is passive. The grammatical
subject of the source passive becomes the `object` slot; the implicit agent
of the passive becomes the `subject` slot.

| Source text | Subject | Predicate | Object |
| --- | --- | --- | --- |
| "Personal data shall be processed lawfully" | "the controller" (CONTEXT) | "shall process lawfully" (CONTEXT) | "personal data" (STATED) |
| "Controllers shall implement appropriate measures" | "controllers" (STATED) | "shall implement" (STATED) | "appropriate measures" (STATED) |
| "Processing of special categories shall be prohibited" | "the controller" (CONTEXT) | "shall not process" (CONTEXT) | "special categories of personal data" (STATED) |
| "Personal data shall be collected for specified purposes" | "the controller" (CONTEXT) | "shall collect for specified purposes" (CONTEXT) | "personal data" (STATED) |
| "Data subjects shall have the right to access" | "data subjects" (STATED) | "shall have" (STATED) | "the right to access" (STATED) |

**Method rule** (applies independently to subject AND predicate, and to all
slots): method marks WHERE the value text came from, NOT which slot it
landed in.

- **STATED** when the value's text (or close paraphrase) appears in the
  paragraph itself — including a value the LLM relocated from the
  grammatical subject of a passive source to the `object` slot.
- **CONTEXT** when the value was inferred from passive-voice transformation
  ("shall process" from "shall be processed"), drawn from a sibling
  paragraph, or pulled from the parent chapeau.
- **CITATION** when the value was lifted from a cross-referenced provision.

So in row 1 above: "personal data" is STATED in `object` because the text
"Personal data" literally appears in the paragraph, even though we moved it
out of the grammatical subject slot. "the controller" is CONTEXT because
the actor is implicit in the passive voice, not named.

Subject and object are multi-valued for genuinely coordinated structures —
one ExtractedValue per distinct value.

### 3.2 Modality from the modal verb

Modality is determined separately from the modal verb (active or passive
form):

| Modal verb | Modality |
| --- | --- |
| "shall", "must", "is required to" | OBLIGATION |
| "shall not", "is prohibited from" | PROHIBITION |
| "may", "is permitted to" | PERMISSION |
| "need not", "is not required to" | DISPENSATION |

### 3.3 Manner adverbs belong in the predicate, not the condition

Manner adverbs describe HOW the action is performed (lawfully, fairly,
transparently, in a transparent manner). They are part of the predicate.

Conditions describe WHEN / IF / UNLESS the obligation applies (preconditions
or qualifying circumstances).

| Phrase | Slot |
| --- | --- |
| "lawfully, fairly and in a transparent manner" | predicate ("shall process lawfully, fairly and in a transparent manner") |
| "for specified, explicit and legitimate purposes" | object qualifier ("shall collect for specified purposes") OR condition if it gates applicability — read in context |
| "where processing is necessary for preventive medicine" | condition |
| "subject to the safeguards in paragraph 3" | condition |
| "by way of derogation from Article 9(1)" | condition (with `references` populated from parent rule per §3.7) |

Test: does the phrase change WHEN the duty applies, or just HOW it's
performed? When-changing → condition. How-describing → predicate.

### 3.4 Multi-value vs. split

Multi-value emission is reserved for **coordinated structures** where
multiple values share the rest of the statement:

- Multiple subjects sharing one predicate–object pair
- Multiple predicates sharing one object ("shall implement and maintain
  appropriate measures")
- Multiple objects sharing one predicate

If a paragraph contains multiple predicates with **independent** objects
(each predicate acts on a distinct object/condition, not a shared one),
emit **one DEONTIC candidate per predicate–object pair**, not a single
merged candidate with parallel-list predicates and objects.

### 3.5 Modality of exemptions — invert the parent rule

- Parent PROHIBITION → exception PERMISSION.
- Parent OBLIGATION → exception DISPENSATION.

Do NOT default every "shall not apply where" exception to DISPENSATION; read
the parent rule's modality first, then flip.

### 3.6 Subject inheritance for exemptions

For a PERMISSION or DISPENSATION carve-out, the subject is inherited from
the parent rule (typically the controller), with method CONTEXT. Safeguard
actors mentioned in the carve-out conditions (e.g., "processed by a
professional subject to professional secrecy") belong in `condition`, NOT
in `subject`. They are conditions on when the carve-out applies.

Worked example — Art 9(2)(h):
- Parent Art 9(1): "Processing of personal data revealing… shall be
  prohibited" (PROHIBITION on the controller).
- Carve-out Art 9(2)(h): "Paragraph 1 shall not apply if processing is
  necessary for the purposes of preventive or occupational medicine…
  subject to the conditions and safeguards in paragraph 3."
- Correct: modality=PERMISSION, subject="the controller" (CONTEXT),
  predicate="may process" (CONTEXT), object="special categories of personal
  data" (CONTEXT), condition=full carve-out trigger including the
  professional-secrecy clause (STATED).

### 3.7 Exemption references

PERMISSION / DISPENSATION carve-outs MUST include the parent rule's IRI in
`references`. The parent chain in the prompt surfaces this on a "references
cited in this lead-in:" line. Any safeguards / condition-source IRIs from
the statement's own cross-references stack on top — both belong.

### 3.8 `severity` rubric

| Level | Trigger |
| --- | --- |
| **high** | Fines-tier (GDPR Art 83(5)/(6); AI Act Art 99(3)/(4)); conditions lawfulness of processing or placement on the market. |
| **medium** | Procedural / documentation / notification obligations whose breach is fineable but not market-blocking. |
| **low** | Housekeeping (record-keeping form, retention metadata, staff designations). |

## 4. DEFINITIONAL Schema and Conventions

- `term`: the term being defined, in canonical form ("personal data", not
  "data"; "biometric data", not "biometrics"). Match the regulation's
  wording.
- `definition`: ExtractedValue with the definitional text and method.
- See §1.1 for what is NOT DEFINITIONAL (pointer-only and forward-reference
  patterns).

### 4.1 Completeness — extract the WHOLE definitional clause

`definition.value` MUST contain the complete definitional clause, including
every "which …", "that …", "such as …" sub-clause that qualifies the term.
Do NOT truncate at internal commas, semicolons before another sub-clause, or
relative-pronoun boundaries — the legally load-bearing content is often in
the second half of the clause.

Worked examples — the **full** definition is what goes in `definition.value`:

- AI Act Art 3(1) "AI system":
  > "a machine-based system that is designed to operate with varying levels
  > of autonomy and that may exhibit adaptiveness after deployment, **and
  > that, for explicit or implicit objectives, infers, from the input it
  > receives, how to generate outputs such as predictions, content,
  > recommendations, or decisions that can influence physical or virtual
  > environments**"

  The bolded inference clause is the operative discriminator — it is what
  distinguishes an AI system from ordinary software. Cutting at "after
  deployment" produces a definition that no longer matches the regulation.

- GDPR Art 4(14) "biometric data":
  > "personal data resulting from specific technical processing relating to
  > the physical, physiological or behavioural characteristics of a natural
  > person, **which allow or confirm the unique identification of that
  > natural person, such as facial images or dactyloscopic data**"

  The bolded clause is the operative discriminator — wearable physiological
  signals are only biometric data under Art 9 once processed to uniquely
  identify. Cutting at "natural person" produces the wrong definition.

- GDPR Art 4(15) "data concerning health":
  > "personal data related to the physical or mental health of a natural
  > person, **including the provision of health care services, which reveal
  > information about his or her health status**"

The clause-boundary heuristic: if the source ends the term-introduction with
a semicolon (the end-of-definition marker in EU regulatory drafting), that
semicolon is the stopping point. Internal commas and relative clauses are
inside the definition, not outside it.

When the source paragraph packs multiple distinct definitions separated by
semicolons (e.g. GDPR Art 4(1) defines both "personal data" AND
"identifiable natural person"), emit one DEFINITIONAL candidate per
distinct term — but each candidate's `definition.value` carries the full
clause for its own term.

## 5. APPLICABILITY Schema and Conventions

### 5.1 Scope-type heuristics

| Phrasing trigger | scope_type |
| --- | --- |
| Establishment, residence, location ("established in the Union") | **TERRITORIAL** |
| Data type, processing activity ("processing of personal data") | **MATERIAL** |
| Natural-vs-legal-person, class of natural persons | **PERSONAL** |
| Date, transition period ("from <date>", "until <date>") | **TEMPORAL** |

Article 3 of GDPR is canonically TERRITORIAL; do not let an incidental
mention of "natural persons" pull the classification to PERSONAL.

### 5.2 `applies_to` ≠ actor

`applies_to` is the **domain / data-type / activity / category** the scope
qualifies. It is NEVER an actor or role. If the candidate `applies_to` comes
out as "Member States", "controllers", or "the Commission", re-examine: the
actor is the *agent* of the scope rule, while the *thing scoped* is the
rules, the processing, or the category being qualified.

### 5.3 List-introducer conditions

When `condition` is a connective that forward-references a list of sub-items
("both of the following conditions are fulfilled", "any of the following
areas"), `references` MUST include the IRIs of **all** listed sub-items, not
just the first.

## 6. NOT_APPLICABLE Stubs — One Per Paragraph

A paragraph that carries no extractable statement yields **exactly one** NA
record. The classifier may emit several NA candidates (one per sentence of a
long non-operative recital); orchestration **merges them into a single NA
record** per paragraph. A non-operative recital is one NA node, never a blob
of near-identical ones.

`statement = {"text": <full paragraph text>}`. Because there is only one NA
per paragraph, the full text is the honest record of what was deemed
non-applicable — no duplication. The merged record's
`classification_rationale` joins the distinct candidate rationales (` | `
separated) for the audit trail.

A paragraph can still carry a mix — one merged NA plus genuine non-NA
statements (e.g. rct_15 → one NA + two APPLICABILITY). Only the NA candidates
collapse; APPLICABILITY / DEFINITIONAL / DEONTIC records are emitted
individually.

**Alignment**: the regression harness keys NA records on `paragraph_iri`
alone (the NA text is LLM-side and not stable enough to match on). Statement
text is therefore not graded for NA records — only that the class is
NOT_APPLICABLE and that exactly one NA appears per paragraph.

## 7. `applies_to_healthcare` — MigrainePredict relevance

True iff MigrainePredict's controller must satisfy / reason with this
provision when operating the system. MigrainePredict processes biometric /
health data of identifiable natural persons and is a high-risk AI system on
the medical-device pathway (Annex III §1 / AI Act Art 6(1)).

**TRUE for**:
- Foundational definitions of personal data, processing, identifiable
  natural person, controller, processor.
- Biometric / genetic / health / special-category data definitions and
  rules.
- General processing principles (Art 5) and lawful basis (Art 6, Art 9).
- Data-subject rights (Arts 12–22).
- Controller obligations (Arts 24, 25, 30, 32, 35).
- DPO / DPIA provisions.
- AI Act high-risk classification, provider obligations, deployer
  obligations.

**FALSE for**:
- Sector carveouts MP does not engage in (employment Art 88, journalism Art
  85, research Art 89, public authorities Art 86, religious associations
  Art 91).
- Internal regulatory machinery (Commission powers, delegated acts, EDPB
  governance).
- Final / commencement provisions.

### 7.1 Rule-based gate (post-LLM override)

The orchestration runs a profile-keyword scan on each LLM-emitted `True`
flag. Three override paths exist:

1. **APPLICABILITY EXCLUDES polarity** → override to False (carve-outs are
   not in-scope).
2. **APPLICABILITY applies_to = "legal persons"** → override to False
   (MP's subjects are natural persons).
3. **No profile-dimension keyword match** in the scoped text → override to
   False with audit note.

Scoped text per class:
- DEONTIC: subject + object + condition
- APPLICABILITY: applies_to + condition
- DEFINITIONAL: term + definition

Profile dimensions are defined in `extract.py:PROFILE_KEYWORDS`:
**lawful_basis**, **data_categories**, **ai_act_risk_vector**.

All overrides set `needs_review = True` and write an audit field
`profile_gate_override`.

## 8. Review gates

Two independent triggers, both setting `needs_review = True`:

### 8.1 Confidence threshold

`confidence < 0.7` → flag. The LLM's self-rated confidence is known to be
weakly calibrated but it's a useful cheap signal.

### 8.2 Groundedness gate *(frozen scope)*

Independent of confidence. Fires only on **CITATION**-sourced
core-identity fields:

- DEONTIC: `subject` or `predicate` method is CITATION.
- DEFINITIONAL: `definition` method is CITATION.
- APPLICABILITY: `applies_to` method is CITATION.

**CONTEXT is NOT a flag.** Passive-voice GDPR puts duty-bearer in CONTEXT
by design (§3.1); flagging CONTEXT would collapse the review queue's
signal-to-noise. CITATION specifically catches the case where
subject/predicate were reconstructed from a cited neighbouring provision
rather than the paragraph or its structural context.

## 9. IRI Scheme

Current canonical scheme (paragraph level):

```
<source>:<unit_id>/<segments>
```

Where:
- `<source>` ∈ {`gdpr`, `aiact`}
- `<unit_id>` is `art_N`, `rct_N`, or `anx_X`
- `<segments>` is `par_N` / `pt_X` / `cont_N` (paragraph / point /
  continuation), nested as the regulation's hierarchy dictates

Examples: `gdpr:art_5/par_1/pt_a`, `gdpr:art_4/par_14`,
`aiact:anx_III/p_0`.

### 9.1 Sub-paragraph statement IDs *(proposed, not yet shipped)*

To represent intra-paragraph references (e.g. Art 5(1)(b)'s archiving
PERMISSION pointing at the sibling PROHIBITION), the scheme will extend to:

```
<paragraph_iri>#<class>_<n>
```

Where `<class>` ∈ {`d`, `def`, `app`, `na`} and `<n>` is the 1-indexed
position among same-class candidates in stage-1 emission order. See the
implementation plan in conversation notes.

## 10. Change log

Conventions in this guide override earlier prompt patches. When this guide
disagrees with a prompt section, the guide wins and the prompt section is
out of date.

- DEONTIC statements are normalised to active voice throughout — subject =
  duty-bearer, predicate transformed from passive when source is passive
  (§3.1). Earlier rounds locked subject only and left predicates passive,
  producing incoherent compositions ("the controller shall be processed").
- Method marks the SOURCE of the value text, not the schema slot it landed
  in (§3.1). "Personal data" relocated from grammatical subject to `object`
  stays STATED — the text is in the paragraph regardless of which slot we
  put it in.
- Manner adverbs (lawfully, fairly, transparently) belong in the predicate,
  not the condition (§3.3). Conditions are when/if/unless gates, not
  how-descriptions.
- DEFINITIONAL `definition.value` must carry the complete clause, including
  every "which …" / "that …" sub-clause up to the semicolon (§4.1). Earlier
  rounds truncated AI Act Art 3(1) and GDPR Art 4(14) at semantically
  load-bearing boundaries.
- Classifier rationale describes class only, never a specific modality
  (§1.1). Pre-committing to "dispensation" contradicts a downstream
  PERMISSION flip on parent-PROHIBITION exemptions.
- Groundedness gate scope narrowed to CITATION only (§8.2) — earlier
  CONTEXT-triggered version flooded the queue with structural inferences.
- NA is one record per paragraph (§6). The classifier's per-sentence NA
  candidates are merged in orchestration; the harness aligns NA on
  paragraph_iri alone. Supersedes the earlier anchor-per-NA scheme, which
  re-introduced the multi-NA churn it was meant to prevent.
