"""Regression assertions for the defined-term tag pass in
build_content_candidates.py, run against actual pipeline data (the
text-carrying records in the canonical run dirs). Run: python test_tag_pass.py
(exit non-zero on any failure).

The standing pair: GDPR Art 5(1)(e)'s ('storage limitation') tag MUST surface
eu-gdpr:StorageLimitationPrinciple on the condition.gdpr vocabulary (the anchor
the manual review had to rescue), and AI Act Art 12(1)'s plain-parenthesis
"(logs)" must NOT count as a tag.
"""
import json
import sys

from build_content_candidates import (TAG_RE, collect_values,
                                      tag_label_matches, load_targets,
                                      build_idf)

CONFLICT_RUN = "data/conflict_pair_run"
DEV_RUN = "data/dev_5run_deontic_pred/run1.extracted.jsonl"

failures = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r}")
    if not ok:
        failures.append(name)


# 1. tag extraction from the records' own carried text (conflict-pair run)
_, _, texts = collect_values([CONFLICT_RUN])
check("pt_e record text present", "gdpr:art_5/par_1/pt_e" in texts, True)
check("pt_e tag extracted from record text",
      TAG_RE.findall(texts["gdpr:art_5/par_1/pt_e"]), ["storage limitation"])
check("plain parens '(logs)' in art_12 record is not a tag",
      TAG_RE.findall(texts["aiact:art_12/par_1"]), [])

# 2. tag extraction generalises across the Art 5(1) principle tags present in
#    the actual DEV run records
dev_texts = {}
for line in open(DEV_RUN):
    r = json.loads(line)
    if r.get("paragraph_text"):
        dev_texts[r["paragraph_iri"]] = r["paragraph_text"]
for iri, want in [("gdpr:art_5/par_1/pt_a", ["lawfulness, fairness and transparency"]),
                  ("gdpr:art_5/par_1/pt_b", ["purpose limitation"])]:
    check(f"tag on {iri} (dev run record)",
          TAG_RE.findall(dev_texts.get(iri, "")), want)

# 3. tag -> label matching against the real routed vocabularies
targets = load_targets()
cond_gdpr_lemmas, _ = build_idf(targets[("condition", "gdpr")])
check("'storage limitation' names the anchor concept",
      tag_label_matches("storage limitation", cond_gdpr_lemmas),
      ["eu-gdpr:StorageLimitationPrinciple"])
check("'purpose limitation' names its principle",
      tag_label_matches("purpose limitation", cond_gdpr_lemmas),
      ["eu-gdpr:PurposeLimitationPrinciple"])
check("nonsense tag matches nothing",
      tag_label_matches("frobnication clause", cond_gdpr_lemmas), [])

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all tag-pass assertions passed")
