"""
Chunk EUR-Lex XHTML manifestations of the AI Act and GDPR into
article/recital-level records suitable for deontic extraction.

Usage:
    python chunker.py                       # processes both files in data/
    python chunker.py data/gdpr.html ...    # processes the given files
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator, Literal

from bs4 import BeautifulSoup, Tag

UnitType = Literal["recital", "article", "citation", "annex", "final_provision"]

# Maps filename stem -> regulation metadata. Stem matching keeps the CLI
# robust to relative/absolute paths without a brittle substring check.
SOURCES: dict[str, dict[str, str]] = {
    "gdpr": {
        "celex": "32016R0679",
        "eli": "http://data.europa.eu/eli/reg/2016/679/oj",
    },
    "aiact": {
        "celex": "32024R1689",
        "eli": "http://data.europa.eu/eli/reg/2024/1689/oj",
    },
}

# ELI markup conventions on EUR-Lex: each leaf regulatory unit lives in a
# div.eli-subdivision whose id encodes the unit type and number.
_ID_PATTERNS: dict[UnitType, re.Pattern[str]] = {
    "recital":         re.compile(r"^rct_(\d+)$"),
    "article":         re.compile(r"^art_(\d+)$"),
    "citation":        re.compile(r"^cit_(\d+)$"),
    "annex":           re.compile(r"^anx_([IVX]+|\d+)$"),
    "final_provision": re.compile(r"^fnp_(\d+)$"),
}

DEFAULT_PATHS = [Path("data/gdpr.html"), Path("data/aiact.html")]


@dataclass
class Chunk:
    source: str            # "gdpr" | "aiact"
    celex: str
    eli: str
    unit_type: UnitType
    unit_id: str           # e.g. "art_32"
    unit_number: str       # e.g. "32"
    chapter: str | None    # e.g. "IV"
    section: str | None    # e.g. "2"
    heading: str | None    # e.g. "Security of processing"
    text: str              # paragraphs joined by blank lines
    paragraphs: list[str] = field(default_factory=list)


def _classify(div: Tag) -> tuple[UnitType, str] | None:
    div_id = div.get("id") or ""
    for unit_type, pattern in _ID_PATTERNS.items():
        m = pattern.match(div_id if isinstance(div_id, str) else "")
        if m:
            return unit_type, m.group(1)
    return None


def _paragraphs(div: Tag) -> list[str]:
    out: list[str] = []
    for p in div.find_all("p", class_="oj-normal"):
        text = " ".join(p.get_text(" ", strip=True).split())
        # Skip lone enumeration markers like "(1)" that EUR-Lex emits in
        # their own <p> alongside the paragraph body.
        if text and not re.fullmatch(r"\(\d+\)", text):
            out.append(text)
    return out


def _walk_annex_table(table: Tag) -> list[str]:
    """Each row pairs a marker cell with a content cell; emit `<marker> <body>`,
    then recurse into nested tables so sub-items appear as their own paragraphs."""
    out: list[str] = []
    tbody = table.find("tbody", recursive=False) or table
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        marker = " ".join(tds[0].get_text(" ", strip=True).split())
        body_parts: list[str] = []
        for el in tds[1].children:
            if not isinstance(el, Tag):
                continue
            if el.name == "p" and "oj-normal" in (el.get("class") or []):
                t = " ".join(el.get_text(" ", strip=True).split())
                if t:
                    body_parts.append(t)
        body = " ".join(body_parts)
        if marker and body:
            out.append(f"{marker} {body}")
        elif body:
            out.append(body)
        for nested in tds[1].find_all("table", recursive=False):
            out.extend(_walk_annex_table(nested))
    return out


def _annex_paragraphs(div: Tag) -> list[str]:
    """Annexes use mixed EUR-Lex layouts: oj-normal lead-ins, nested tables for
    list items (Annex III), and inline-paragraph divs (Annex VI). Walk direct
    children so each item appears once in document order."""
    out: list[str] = []
    for child in div.children:
        if not isinstance(child, Tag):
            continue
        classes = child.get("class") or []
        if child.name == "p" and "oj-normal" in classes:
            text = " ".join(child.get_text(" ", strip=True).split())
            if text:
                out.append(text)
        elif child.name == "table":
            out.extend(_walk_annex_table(child))
        elif child.name == "div" and "oj-enumeration-spacing" in classes:
            text = " ".join(child.get_text(" ", strip=True).split())
            if text:
                out.append(text)
    return out


def _annex_heading(div: Tag) -> str | None:
    """ANNEX containers carry an "ANNEX <N>" label followed by the title in a
    second oj-doc-ti paragraph; return the title."""
    titles = [p for p in div.find_all("p", class_="oj-doc-ti", recursive=False)]
    if len(titles) >= 2:
        return " ".join(titles[1].get_text(" ", strip=True).split())
    return None


def _enclosing(div: Tag, prefix: str) -> str | None:
    """Walk up the DOM to find the chapter/section id segment containing div."""
    node = div.parent
    while isinstance(node, Tag):
        cid = node.get("id") or ""
        if isinstance(cid, str):
            for segment in cid.split("."):
                if segment.startswith(f"{prefix}_"):
                    return segment[len(prefix) + 1:]
        node = node.parent
    return None


def _heading(div: Tag) -> str | None:
    for cls in ("oj-sti-art", "oj-ti-art"):
        node = div.find("p", class_=cls)
        if node:
            return " ".join(node.get_text(" ", strip=True).split())
    return None


def chunk_xhtml(html: str, source: str, celex: str, eli: str) -> Iterator[Chunk]:
    """Yield one Chunk per leaf regulatory unit found in the document."""
    soup = BeautifulSoup(html, "html.parser")
    for nav in soup.find_all("nav"):
        nav.decompose()
    for toc in soup.find_all(id="TOC"):
        toc.decompose()

    # Annexes use class="eli-container" instead of eli-subdivision; include
    # both so _classify's id-pattern filter does the actual selection.
    candidates = soup.find_all(
        "div", class_=lambda c: bool(c) and ("eli-subdivision" in c or "eli-container" in c)
    )
    for div in candidates:
        classification = _classify(div)
        if classification is None:
            continue
        unit_type, unit_number = classification
        if unit_type == "annex":
            paragraphs = _annex_paragraphs(div)
            heading = _annex_heading(div)
        else:
            paragraphs = _paragraphs(div)
            heading = _heading(div)
        if not paragraphs:
            continue
        yield Chunk(
            source=source,
            celex=celex,
            eli=eli,
            unit_type=unit_type,
            unit_id=str(div.get("id") or ""),
            unit_number=unit_number,
            chapter=_enclosing(div, "cpt"),
            section=_enclosing(div, "sct"),
            heading=heading,
            text="\n\n".join(paragraphs),
            paragraphs=paragraphs,
        )


def _source_for(path: Path) -> tuple[str, dict[str, str]]:
    stem = path.stem.lower()
    for key, meta in SOURCES.items():
        if key in stem:
            return key, meta
    raise SystemExit(
        f"unknown source for {path}; filename stem must contain one of {list(SOURCES)}"
    )


def process(path: Path) -> Path:
    key, meta = _source_for(path)
    chunks = list(chunk_xhtml(path.read_text(encoding="utf-8"), key, meta["celex"], meta["eli"]))

    out_path = path.parent / f"{path.stem}.chunks.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

    by_type: dict[str, int] = {}
    for c in chunks:
        by_type[c.unit_type] = by_type.get(c.unit_type, 0) + 1
    summary = ", ".join(f"{t}={n}" for t, n in sorted(by_type.items()))
    print(f"{path.name}: {len(chunks)} chunks ({summary}) -> {out_path}")
    return out_path


def main(argv: list[str]) -> None:
    paths = [Path(p) for p in argv] if argv else DEFAULT_PATHS
    for p in paths:
        process(p)


if __name__ == "__main__":
    main(sys.argv[1:])
