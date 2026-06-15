# Gold Set — Annotation Notes & Adjudication Record

Three files work together:

| File | Role |
|---|---|
| `gold_set.jsonl` | 23 frozen reference records — the answer key. Inputs were already fixed; this is what was missing. |
| `compare_to_gold.py` | Diffs a fresh extractor run against the gold set, field by field. Run it every time. |
| `gold_annotation_notes.md` | This file: the conventions encoded, and the judgment calls to confirm. |

Source text was pulled **verbatim** from the GDPR file in the project (`L_2016119EN_01000101_xml.html`) and from the official AI Act (Reg. 2024/1689) for Art 3(1), 6(1), Annex III. The definition `value` fields are exact source strings — this is deliberate, because the regressions that kept slipping through were silent truncations of definitions.

---

## 1. How grading works (the two tiers)

Not every field has a single right answer, so the harness grades them differently.

**OBJECTIVE — one correct answer. Mismatch = HARD FAIL (breaks the build).**
`statement_class`, `term`, `definition.value`, `source_article`, `references` (compared as a set), `scope_type`, `polarity`, `applies_to.value`, `modality`. These are what catch truncations, wrong IRIs, dropped references, modality flips, mis-classification.

**INTERPRETIVE — reasonable wording/judgment may differ. Mismatch = SOFT FLAG (surfaced, not failed).**
`subject`, `predicate`, `object`, `condition`, `beneficiary`, `applies_to_healthcare`, `severity`. The gold value here is *your adjudicated convention*, not ground truth. A flag means "this drifted from your convention — look at it," not "this is wrong." This is how the subject/voice oscillation gets surfaced run-to-run without false build breaks.

**IGNORED — model-side signals, not ground truth.**
`confidence` (never graded — it is the model's self-report). `needs_review` (graded **only** on the three records where the profile gate produces a deterministic outcome; elsewhere it is gate-evaluation, handled separately). `classification_rationale` and `anchor` are provenance, not graded.

> This split is the answer to "how can there be a gold answer when half of these are judgment calls?" — objective fields are gold; interpretive fields freeze *your* decision so deviation is visible.

---

## 2. Conventions encoded in the gold set

These are the rules we settled across the review rounds, now written down so they stop drifting:

1. **Recitals — no operative deontic.** A recital's "should"/"may" is interpretive, not binding, so recitals never emit DEONTIC. They emit APPLICABILITY/DEFINITIONAL **only** where the statement mirrors an operative Article's scope (e.g. rct_15 ≈ Art 2 material scope → kept). Recital statements that are purely about legislative interplay (rct_10, Member-State margin) → `NOT_APPLICABLE`. rct_8, rct_13 → `NOT_APPLICABLE`.

2. **Exception clauses ("X shall not apply where Y") are deontic, not scope.** Classify by the parent rule: exception to a **PROHIBITION → PERMISSION**; exception to an **OBLIGATION → DISPENSATION**. Both current carve-outs (Art 9(2)(h), Art 5(1)(b) archiving) are exceptions to prohibitions → **PERMISSION**. *No record in this set is an exception-to-an-obligation, so `DISPENSATION` correctly appears nowhere.*

3. **Forward-reference "definitions" are APPLICABILITY, not DEFINITIONAL.** If the definition body is an empty connective ("where the following conditions are fulfilled", "listed in the following areas"), it is a scope clause (Art 6(1), Annex III chapeau).

4. **`method` marks provenance, not slot.** `STATED` = the value appears in the text; `CONTEXT` = recovered from the regulatory frame; `CITATION` = recovered from a cross-referenced provision. "personal data" is `STATED` wherever it sits, even as an object.

5. **`applies_to_healthcare`.** True if MigrainePredict's controller must satisfy the provision when operating the system: foundational definitions (personal data, processing, identifiable person, biometric, health), general processing principles (Art 5), lawful-basis provisions (Art 9) → TRUE. A rule-based profile gate then overrides to FALSE when no profile dimension (`lawful_basis`, `data_categories`, `ai_act_risk_vector`) is touched, and queues those for review. EXCLUDES-polarity scope carve-outs → FALSE (the carve-out is definitionally out-of-scope).

6. **`references` carry the parent rule.** For exceptions, the parent prohibition/obligation IRI goes in `references` alongside any safeguards cross-ref (Art 9(2)(h) → `art_9/par_1` + `art_9/par_3`).

---

## 3. Judgment calls to confirm with a domain reviewer

You said the legal detail isn't yours to adjudicate — so these are the specific cells where I committed to a defensible-but-contestable reading. A supervisor or legal reviewer should sign off on these before the gold set is treated as final; everything else is mechanical.

- **G15–G20 subject/voice convention.** I committed to the **active duty-bearer** framing: `subject = "the controller"` (CONTEXT, since GDPR's passive voice never names it), predicate in active voice, modality carrying the deontic force. The alternative was passive-faithful (`subject = "Personal data"`, STATED). Pick one institutionally; I picked active because it makes the compliance graph queryable by *who must act*. Applied uniformly across all six deontic records.

- **G15 manner placement.** "lawfully, fairly and in a transparent manner" is folded into the **predicate** (it is *how*, not a precondition), not `condition`.

- **G18 / G20 modality.** Both scored **PERMISSION** per rule 2. This is the standard deontic reading (carve-out from a prohibition), but note some legal-NLP work treats Art 9(2) derogations as a distinct "derogation" category — worth one sentence anchored to Galli/your deontic source rather than to our chat.

- **`severity`.** Obligations/prohibitions = `high`; the archiving PERMISSION = `medium`. This axis is generic and the assignments are adjustable.

- **G02 / rct_10.** I suppressed rct_10's applicability content to a single `NOT_APPLICABLE` (legislative-interplay = interpretive). The earlier rounds had extracted it as applicability. This is the one borderline recital call.

---

## 4. Known gaps deferred (your sub-statement-id bucket)

- **G18 parent reference.** The Art 5(1)(b) archiving PERMISSION is an exception to the *sibling prohibition in the same paragraph IRI*. With no sub-statement id, the parent link is unrepresentable; `references` holds only `art_89/par_1`. Both fall out of one fix: a sub-statement id scheme (e.g. `gdpr:art_5/par_1/pt_b#s3`).
- **G23 Annex III chapeau.** Inert list header. The eight high-risk areas want per-area extraction (Area 1, biometrics, is MigrainePredict's). Same sub-id fix enables it.

---

## 5. Using it each run

```
python compare_to_gold.py gold_set.jsonl your_new_run.jsonl
```

Exit code is non-zero on any HARD failure, any missing gold record, or any extra/duplicate run record — so it can sit in a pre-commit hook or CI step. Soft flags print but do not fail. When you intentionally change a convention, update the gold record in the same commit; the diff then documents the change instead of nagging about it.

> Note: `modality` is also part of the within-paragraph match key, so a modality *flip* surfaces as a MISSING + EXTRA pair rather than a single field diff (visible in the self-test). Still caught — just read those two lines together.
