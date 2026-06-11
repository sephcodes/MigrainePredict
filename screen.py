"""
Binary LLM screener for the candidate paragraphs from the GDPR and AI Act.

For each paragraph, the LLM decides whether the text carries substantive content
for downstream deontic-statement extraction (KEEP if it contains a substantive
deontic norm, a scope clause, an applicability condition, or a substantive
definition; DISCARD otherwise).

Default backend is Gemini 2.5 Flash via the Google AI Studio API; Mistral Nemo
via Ollama remains available with `--backend mistral`.

Output is flat: one record per paragraph, every input paragraph annotated
with `screen_keep` and `screen_justification` (Galli's vocabulary).

To respect Gemini's 1,000 requests/min and 1M input-tokens/min limits, work
is batched (500 paragraphs per batch, with concurrent in-batch requests via a
thread pool) and a 60-second pause is taken between batches and between files.

Usage:
    python screen.py                                       # process both files (Gemini)
    python screen.py --backend mistral                     # use Ollama mistral-nemo
    python screen.py --limit 10                            # smoke-test 10 paragraphs per file
    python screen.py --limit 10 data/gdpr.candidates.jsonl # smoke a single file
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from pathlib import Path
from typing import Iterator

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from tqdm import tqdm

DEFAULT_PATHS = [Path("data/gdpr.candidates.jsonl"), Path("data/aiact.candidates.jsonl")]
MISTRAL_MODEL = "mistral-nemo"
GEMINI_MODEL = "gemini-2.5-flash"

# Rate-limit pacing. Gemini 2.5 Flash via AI Studio gives 1,000 req/min and
# 1M input-tokens/min; we send 500 per batch then pause 60s, which keeps us
# comfortably under both limits even when batches finish in tens of seconds.
BATCH_SIZE = 500
BATCH_WAIT_SECONDS = 60
MAX_CONCURRENT = 20
# Bound how long any single API call can hang for. Gemini occasionally leaves
# a connection open without progress; without a timeout the whole batch stalls.
REQUEST_TIMEOUT_SECONDS = 60
# Catastrophic backstop on a whole batch — if many requests stall at once, we
# stop waiting after this many seconds and flush partial results.
BATCH_DEADLINE_SECONDS = 300


def _load_dotenv() -> None:
    """Read .env into os.environ for keys not already set. Lightweight to avoid
    a hard dependency on python-dotenv."""
    import os
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


class ScreenDecision(BaseModel):
    """Single-paragraph keep/discard decision with a short justification."""

    screen_keep: bool = Field(
        description=(
            "True iff the paragraph carries a substantive deontic norm, a scope "
            "clause, an applicability condition, or a substantive definition."
        )
    )
    screen_justification: str = Field(
        description="One short sentence (under 25 words) explaining the decision."
    )


SYSTEM_PROMPT = (
    "You classify single paragraphs from EU regulatory text (GDPR and AI Act) as "
    "KEEP or DISCARD for downstream deontic-statement extraction.\n\n"
    "The paragraph may be a sub-item of an enumerated list; in that case the "
    "lead-in clauses above it are provided as 'Parent context'. Read the "
    "paragraph together with its parent context — the operative verb and "
    "subject of a deontic norm often live in the parent.\n\n"
    "KEEP the paragraph if it contains (or, combined with its parent context, "
    "contributes) any of:\n"
    "  - a substantive deontic norm (obligation, permission, or prohibition "
    "addressed to a regulatory subject like a controller, processor, provider, "
    "deployer, or Member State)\n"
    "  - a material, personal, or territorial scope clause\n"
    "  - a substantive applicability test ('applies where...', 'shall not apply to...')\n"
    "  - a substantive definition of a regulatory term, including a recital "
    "that supplies definitional criteria (e.g., identifiability tests, "
    "scope-determining factors)\n\n"
    "DISCARD the paragraph if its only substantive content is one of:\n"
    "  - *Preamble or policy framing*: motivation, historical context, "
    "fundamental-rights basis, why the regulation exists. Describes what the "
    "regulation is about, not what it requires.\n"
    "  - *Internal regulatory machinery*: how the EU administers the regulation "
    "— delegated-act conferral/revocation, committee procedures, Commission "
    "notification duties. The addressee is an EU institution, not a regulatory "
    "subject.\n"
    "  - *Temporal applicability / commencement*: entry into force, application "
    "dates, staggered activation by date. Example phrases: 'shall enter into "
    "force on the twentieth day', 'shall apply from 25 May 2018', 'Chapter X "
    "shall apply from <date>'. These state WHEN the regulation takes effect, "
    "not WHO/WHAT/HOW it regulates. Distinguish from substantive applicability "
    "tests like 'applies where...' which state WHO/WHAT.\n"
    "  - *Bare cross-references* without normative content.\n\n"
    "Do not infer norms from silence: discard the paragraph if it does not "
    "explicitly state a norm, scope, applicability test, or substantive definition.\n\n"
    "When the decision is genuinely borderline, default to KEEP — this screener "
    "filters for a downstream extractor that performs finer-grained discrimination. "
    "But a paragraph whose only substantive content falls into a DISCARD category "
    "above is not borderline; discard it.\n\n"
    "Respond with the structured decision and a one-sentence justification "
    "under 25 words."
)

USER_PROMPT = (
    "Source: {source}\n"
    "Unit: {unit_type} {unit_number}{heading_block}\n"
    "{parent_block}"
    "Paragraph:\n{paragraph}"
)


def _heading_block(heading: str | None) -> str:
    return f" — {heading}" if heading else ""


def _parent_block(parent_text: str | None) -> str:
    if not parent_text:
        return ""
    return f"Parent context (lead-in clauses above this paragraph):\n{parent_text}\n\n"


def build_chain(backend: str = "mistral"):
    """Build a screening chain backed by Mistral Nemo via Ollama (`mistral`) or
    Gemini 2.5 Flash via the Google AI Studio API (`gemini`)."""
    import os
    if backend == "mistral":
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model=MISTRAL_MODEL, temperature=0).with_structured_output(ScreenDecision)
    elif backend == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        _load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set; add to .env or export")
        llm = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL,
            temperature=0,
            google_api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ).with_structured_output(ScreenDecision)
    else:
        raise ValueError(f"unknown backend {backend!r}; choose 'mistral' or 'gemini'")
    prompt = ChatPromptTemplate.from_messages(
        [("system", SYSTEM_PROMPT), ("user", USER_PROMPT)]
    )
    return prompt | llm


def iter_paragraphs(path: Path, limit: int | None) -> Iterator[dict]:
    n = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)
            n += 1
            if limit is not None and n >= limit:
                return


def _decide(chain, rec) -> tuple[dict, bool, str, bool]:
    """Call the chain on one record. Fail-open on errors so a transient API
    failure doesn't silently drop substantive text. Returns (rec, keep,
    justification, errored)."""
    try:
        d: ScreenDecision = chain.invoke({
            "source": rec["source"],
            "unit_type": rec["unit_type"],
            "unit_number": rec["unit_number"],
            "heading_block": _heading_block(rec.get("heading")),
            "parent_block": _parent_block(rec.get("parent_text")),
            "paragraph": rec["text"],
        })
        return rec, bool(d.screen_keep), d.screen_justification, False
    except Exception as e:
        return rec, True, f"LLM error (kept by default): {e!s}", True


def screen(path: Path, limit: int | None, backend: str = "gemini") -> Path:
    suffix = ".smoke.jsonl" if limit else ".screened.jsonl"
    out_path = path.with_name(path.name.replace(".candidates.jsonl", suffix))
    chain = build_chain(backend)

    work = list(iter_paragraphs(path, limit))
    total = len(work)
    n_batches = max(1, (total + BATCH_SIZE - 1) // BATCH_SIZE)

    kept = 0
    errors = 0
    with out_path.open("w", encoding="utf-8") as dst:
        for b_idx, batch_start in enumerate(range(0, total, BATCH_SIZE)):
            batch = work[batch_start:batch_start + BATCH_SIZE]
            results: list[tuple[dict, bool, str, bool] | None] = [None] * len(batch)
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
                future_to_idx = {ex.submit(_decide, chain, rec): i for i, rec in enumerate(batch)}
                with tqdm(total=len(batch), desc=f"{path.name} batch {b_idx+1}/{n_batches}") as bar:
                    try:
                        for future in as_completed(future_to_idx, timeout=BATCH_DEADLINE_SECONDS):
                            results[future_to_idx[future]] = future.result()
                            bar.update(1)
                    except FuturesTimeout:
                        n_stuck = sum(1 for r in results if r is None)
                        print(f"  WARN: {n_stuck} requests didn't complete within {BATCH_DEADLINE_SECONDS}s; flushing partial batch")

            # Fill any positions left unresolved (timed-out or cancelled) with errors.
            for i, r in enumerate(results):
                if r is None:
                    rec = batch[i]
                    results[i] = (rec, True, "LLM error (kept by default): batch deadline exceeded", True)

            for rec, keep, justification, errored in results:  # type: ignore[misc]
                if errored:
                    errors += 1
                kept += int(keep)
                dst.write(json.dumps({
                    **rec,
                    "screen_keep": keep,
                    "screen_justification": justification,
                }, ensure_ascii=False) + "\n")
            dst.flush()

            if batch_start + BATCH_SIZE < total:
                print(f"  sleeping {BATCH_WAIT_SECONDS}s before next batch")
                time.sleep(BATCH_WAIT_SECONDS)

    print(f"{path.name}: {kept}/{total} kept ({errors} errors) -> {out_path}")
    return out_path


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--limit", type=int, default=None,
        help="Stop after N paragraphs per file (smoke test). Writes to *.smoke.jsonl.",
    )
    p.add_argument(
        "--backend", default="gemini", choices=["gemini", "mistral"],
        help="LLM backend (default: gemini = Gemini 2.5 Flash via API).",
    )
    p.add_argument("paths", nargs="*", help="Candidate JSONL files; defaults to both data/ candidates.")
    args = p.parse_args(argv)

    paths = [Path(x) for x in args.paths] if args.paths else DEFAULT_PATHS
    for i, path in enumerate(paths):
        if i > 0:
            print(f"\nsleeping {BATCH_WAIT_SECONDS}s between files (rate limit pacing)")
            time.sleep(BATCH_WAIT_SECONDS)
        screen(path, args.limit, args.backend)


if __name__ == "__main__":
    main(sys.argv[1:])
