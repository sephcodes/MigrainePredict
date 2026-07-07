import json
from collections import Counter

gold = json.load(open("data/graphrag/gold_queries.json"))
gold_v = {q["query_id"]: q["gold_verdict"] for q in gold["queries"]}
para_of = {p["query_id"]: p["paraphrase_of"] for p in gold["paraphrases"]}

results = {}
for line in open("data/graphrag/gold_run1.results.jsonl"):
    r = json.loads(line)
    results[r["query_id"]] = r  # last wins if rerun

# --- completeness
expected = set(gold_v) | set(para_of)
missing = expected - set(results)
empty = [q for q, r in results.items() if not r["verdict"].get("verdict")]
print(f"completeness: {len(results)}/{len(expected)} results"
      + (f"  MISSING: {sorted(missing)}" if missing else "  (none missing)")
      + (f"  EMPTY: {empty}" if empty else ""))

# --- agreement
def gv(qid):
    return gold_v[para_of[qid]] if qid in para_of else gold_v[qid]

rows = []
agree_base = agree_para = n_base = n_para = 0
conf = Counter()
for qid, r in sorted(results.items()):
    sys_v, gold_verdict = r["verdict"]["verdict"], gv(qid)
    ok = sys_v == gold_verdict
    conf[(gold_verdict, sys_v)] += 1
    if qid in para_of:
        n_para += 1; agree_para += ok
    else:
        n_base += 1; agree_base += ok
    if not ok:
        rows.append((qid, gold_verdict, sys_v, r["seed_path"]))

print(f"\nagreement: base {agree_base}/{n_base}  paraphrase {agree_para}/{n_para}  "
      f"overall {agree_base+agree_para}/{n_base+n_para}")
print("\nmisses (qid gold -> system, path):")
for qid, g, s, p in rows:
    print(f"  {qid:6} {g:14} -> {s:14} [{p}]")

# --- paraphrase stability: does each trio agree with itself?
unstable = []
for base in sorted({b for b in para_of.values()}):
    trio = [base] + [q for q, b in para_of.items() if b == base]
    vs = {q: results[q]["verdict"]["verdict"] for q in trio if q in results}
    if len(set(vs.values())) > 1:
        unstable.append((base, vs))
print(f"\nparaphrase stability: {10-len(unstable)}/10 trios internally consistent")
for base, vs in unstable:
    print(f"  {base}: {vs}")

# --- seed paths + cypher loop
paths = Counter(r["seed_path"] for r in results.values())
attempts = [a for r in results.values() for a in r["cypher_attempts"]]
errs = sum(1 for a in attempts if a["error"])
print(f"\nseed paths: {dict(paths)}")
print(f"llm cypher attempts: {len(attempts)} total, {errs} failed, across "
      f"{sum(1 for r in results.values() if r['cypher_attempts'])} queries")

# --- confusion matrix
labels = ["COMPLIANT", "NON_COMPLIANT", "INSUFFICIENT", "NOT_APPLICABLE"]
print("\nconfusion (rows=gold, cols=system):")
print("  " + "".join(f"{l[:6]:>8}" for l in labels))
for g in labels:
    print(f"  {g[:6]:<6}" + "".join(f"{conf.get((g, s), 0):>8}" for s in labels))