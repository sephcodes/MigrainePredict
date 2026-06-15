# Held-Out Validation Set — Notes

14 records, paragraphs **not used to develop any prompt rule**. Run your extractor over these same paragraphs and grade with the existing harness:

```
python compare_to_gold.py holdout_gold_set.jsonl your_holdout_run.jsonl
```

## What this validates (and what it doesn't)

It tests whether the rules tuned on the 23-record dev set **generalise to structures they weren't fitted to**. The extractor has never seen these paragraphs, so this is a genuine generalisation check. It does **not** establish gold-label independence — I authored these labels applying the same frozen conventions, so a second annotator (your planned Skein pass) is still the independent check. Use this to decide go/no-go on scaling; use Skein for inter-annotator agreement.

All text is verbatim: GDPR from your project file, AI Act from the EC AI Act Service Desk and EUR-Lex.

## Coverage — every record exercises something the dev set never did

| id | structure under test |
|---|---|
| H01–H02 | MP-critical definitions (controller, consent) — long definitions to re-test the truncation class |
| H03 | lawfulness gate ("shall be lawful only if") — deontic-vs-applicability boundary |
| H04 | multi-point enumeration sibling (Art 9(2)(a)); MP's actual Art 9 basis |
| H05 | **profile-gate precision probe** — special-category keyword present, but not MP's basis |
| H06 | **active named subject** ("The controller shall") — `subject` method STATED, vs the dev set's passive CONTEXT |
| H07 | data-subject **right reframed as controller prohibition** |
| H08 | **DISPENSATION** — exception to an *obligation* (the modality branch absent from the dev set) |
| H09 | temporal qualifier ("72 hours") *inside* an obligation — must not be read as TEMPORAL scope |
| H10 | conditional obligation gated on "high risk" (DPIA; MP is high-risk) |
| H11 | **TEMPORAL** scope_type (pure date) |
| H12 | **AI Act deontic**; duty-bearer is **provider**, not controller |
| H13 | **per-area Annex III** extraction (not the chapeau); second precision probe |
| H14 | **TEMPORAL with per-chapter staggering** (the false-positive cluster in your notes) |

## Two records are designed to fail under the current pipeline — that's the point

`H05` and `H13` are **precision probes**. Both contain a keyword your profile gate matches (special-category / biometric) but neither is MigrainePredict's operative basis — H05 is the research derogation (MP relies on consent), H13 is Annex III biometric *use* (MP is high-risk via the Art 6(1) medical-device route, not standalone biometric identification). Gold marks both `applies_to_healthcare=False`. If your extractor returns `True`, that is the keyword gate over-matching — exactly the failure mode flagged back at the smoke stage, now with a held-out probe to measure it. A pass here means the gate distinguishes keyword presence from operative relevance; a fail tells you the gate needs the operative-basis check, not just term matching.

## Judgment calls (confirm with a domain reviewer before treating gold as final)

- **H03** — I read the Art 6(1)(a) lawfulness gate as a conditional `PERMISSION`. It could instead be MATERIAL applicability ("processing is lawful only if…"). Pick one.
- **H07** — Art 22(1) is written as a data-subject *right*; I reframed it as a controller `PROHIBITION` with the subject as beneficiary, per your duty-bearer convention. Confirm rights are reframed this way.
- **H12** — AI Act duty-bearers are **provider / deployer**, never "the controller". The dev set was GDPR-only, so your subject convention has no AI-Act variant yet. I used `the provider` for Art 14(1). **This is a convention gap to settle before scaling the AI Act half** — decide how provider vs deployer vs controller is assigned, because most AI Act obligations will hit it.
- **H08** — `DISPENSATION` (not PERMISSION), because Art 30(5) excepts an *obligation*. This is the first time the obligation-exception branch is exercised; verify the extractor produces DISPENSATION, not PERMISSION.

## What a clean result looks like

Near-zero HARD on H01–H02, H04, H06, H09, H10, H11, H14 (mechanical) and correct DISPENSATION on H08 → the rules generalise; scale with confidence. Failures concentrated on H03/H07/H12 are *convention* gaps (settle them), not defects. Failures on H05/H13 are the gate-precision signal. A broad HARD spike across mechanical records would mean the dev-set fixes were overfit — better to learn that here than mid-corpus.
