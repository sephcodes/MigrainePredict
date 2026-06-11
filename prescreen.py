"""
Pre-screen chunks before the LLM binary screener.

Discards unit_types that are unambiguously procedural (citation preambles,
final provisions on entry into force) so the LLM call only runs on chunks
where the substantive-vs-procedural distinction actually needs a model.

Usage:
    python prescreen.py                                # processes both files in data/
    python prescreen.py data/gdpr.chunks.jsonl ...     # processes the given files
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_PATHS = [Path("data/gdpr.chunks.jsonl"), Path("data/aiact.chunks.jsonl")]
DROP_TYPES = frozenset({"citation", "final_provision"})


def prescreen(path: Path) -> Path:
    out_path = path.with_name(path.name.replace(".chunks.jsonl", ".candidates.jsonl"))
    kept_by_type: dict[str, int] = {}
    dropped_by_type: dict[str, int] = {}
    with path.open(encoding="utf-8") as src, out_path.open("w", encoding="utf-8") as dst:
        for line in src:
            rec = json.loads(line)
            unit_type = rec.get("unit_type", "")
            if unit_type in DROP_TYPES:
                dropped_by_type[unit_type] = dropped_by_type.get(unit_type, 0) + 1
                continue
            dst.write(line)
            kept_by_type[unit_type] = kept_by_type.get(unit_type, 0) + 1

    kept_summary = ", ".join(f"{t}={n}" for t, n in sorted(kept_by_type.items())) or "none"
    dropped_summary = ", ".join(f"{t}={n}" for t, n in sorted(dropped_by_type.items())) or "none"
    print(
        f"{path.name}: kept {sum(kept_by_type.values())} ({kept_summary}), "
        f"dropped {sum(dropped_by_type.values())} ({dropped_summary}) -> {out_path}"
    )
    return out_path


def main(argv: list[str]) -> None:
    paths = [Path(p) for p in argv] if argv else DEFAULT_PATHS
    for p in paths:
        prescreen(p)


if __name__ == "__main__":
    main(sys.argv[1:])
