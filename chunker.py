"""
Chunk EUR-Lex XHTML manifestations of the AI Act and GDPR into paragraph-level
records suitable for screening and deontic extraction.

Each output record is a single regulatory paragraph (a numbered paragraph, an
enumerated sub-item, or a recital body) annotated with its provenance: source
regulation, regulatory unit (article/recital/annex/...), heading, and a
`parent_text` field carrying the concatenated lead-in clauses above it so the
operative subject is not lost when sub-items are screened in isolation.

Usage:
    python chunker.py                       # processes both files in data/
    python chunker.py data/gdpr.html ...    # processes the given files
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, Literal

from bs4 import BeautifulSoup, Tag

UnitType = Literal["recital", "article", "citation", "annex", "final_provision"]

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

# ELI markup conventions on EUR-Lex: each regulatory unit lives in a div whose
# id encodes the unit type and number.
_ID_PATTERNS: dict[UnitType, re.Pattern[str]] = {
    "recital":         re.compile(r"^rct_(\d+)$"),
    "article":         re.compile(r"^art_(\d+)$"),
    "citation":        re.compile(r"^cit_(\d+)$"),
    "annex":           re.compile(r"^anx_([IVX]+|\d+)$"),
    "final_provision": re.compile(r"^fnp_(\d+)$"),
}

DEFAULT_PATHS = [Path("data/gdpr.html"), Path("data/aiact.html")]


@dataclass
class Paragraph:
    source: str            # "gdpr" | "aiact"
    celex: str
    eli: str
    unit_type: UnitType
    unit_id: str           # e.g. "art_5" — the enclosing regulatory unit
    unit_number: str       # e.g. "5"
    chapter: str | None    # e.g. "IV"
    section: str | None    # e.g. "2"
    heading: str | None    # e.g. "Principles relating to processing of personal data"
    paragraph_index: int   # 0-based index within the unit (document order)
    text: str              # the paragraph text, e.g. "(a) processed lawfully, fairly..."
    parent_text: str | None  # concatenated lead-in chain, or None if top-level


def _classify(div: Tag) -> tuple[UnitType, str] | None:
    div_id = div.get("id") or ""
    for unit_type, pattern in _ID_PATTERNS.items():
        m = pattern.match(div_id if isinstance(div_id, str) else "")
        if m:
            return unit_type, m.group(1)
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


def _annex_heading(div: Tag) -> str | None:
    """ANNEX containers carry an "ANNEX <N>" label then the title in a second
    oj-doc-ti paragraph; return the title."""
    titles = [p for p in div.find_all("p", class_="oj-doc-ti", recursive=False)]
    if len(titles) >= 2:
        return " ".join(titles[1].get_text(" ", strip=True).split())
    return None


def _walk_table_rows(table: Tag, chain: list[str]) -> Iterator[tuple[str, list[str]]]:
    """Yield (row_text, chain) for each row's marker+body merge. Recurse into
    nested tables in the body cell with the row text appended to the chain so
    sub-sub-items carry their immediate enclosing item as additional context."""
    tbody = table.find("tbody", recursive=False) or table
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        marker = " ".join(tds[0].get_text(" ", strip=True).split())
        body_parts: list[str] = []
        for el in tds[1].children:
            if isinstance(el, Tag) and el.name == "p" and "oj-normal" in (el.get("class") or []):
                t = " ".join(el.get_text(" ", strip=True).split())
                if t:
                    body_parts.append(t)
        body = " ".join(body_parts)
        if marker and body:
            row_text = f"{marker} {body}"
        elif body:
            row_text = body
        elif marker:
            row_text = marker
        else:
            row_text = ""
        if row_text:
            yield row_text, chain
            for nested in tds[1].find_all("table", recursive=False):
                yield from _walk_table_rows(nested, chain + [row_text])


def _walk_paragraphs(node: Tag, chain: list[str]) -> Iterator[tuple[str, list[str]]]:
    """Walk a unit (or a paragraph-wrapper div), yielding (text, chain) for each
    terminal paragraph found. `chain` is the list of ancestor lead-in texts.

    A `<p class="oj-normal">` ending with ":" becomes the lead-in for the next
    sibling `<table>` (its rows inherit this lead-in as immediate parent).
    Classless wrapper divs (EUR-Lex's per-paragraph grouping like
    <div id="005.001">) are descended without changing the chain."""
    most_recent_leadin: str | None = None
    for child in node.children:
        if not isinstance(child, Tag):
            continue
        classes = child.get("class") or []
        if "eli-title" in classes:
            continue
        if child.name == "p" and "oj-normal" in classes:
            text = " ".join(child.get_text(" ", strip=True).split())
            # Skip EUR-Lex's lone enumeration markers like "(1)" emitted in
            # their own <p> alongside the recital body.
            if text and not re.fullmatch(r"\(\d+\)", text):
                yield text, list(chain)
                most_recent_leadin = text if text.endswith(":") else None
        elif child.name == "div" and "oj-enumeration-spacing" in classes:
            text = " ".join(child.get_text(" ", strip=True).split())
            if text:
                yield text, list(chain)
                most_recent_leadin = None
        elif child.name == "table":
            sub_chain = chain + ([most_recent_leadin] if most_recent_leadin else [])
            yield from _walk_table_rows(child, sub_chain)
        elif child.name == "div":
            yield from _walk_paragraphs(child, chain)


def chunk_xhtml(html: str, source: str, celex: str, eli: str) -> Iterator[Paragraph]:
    """Yield one Paragraph per regulatory paragraph found in the document."""
    soup = BeautifulSoup(html, "html.parser")
    for nav in soup.find_all("nav"):
        nav.decompose()
    for toc in soup.find_all(id="TOC"):
        toc.decompose()

    # Annexes use class="eli-container" instead of "eli-subdivision"; include
    # both and let _classify's id-pattern filter do the actual selection.
    candidates = soup.find_all(
        "div", class_=lambda c: bool(c) and ("eli-subdivision" in c or "eli-container" in c)
    )
    for div in candidates:
        classification = _classify(div)
        if classification is None:
            continue
        unit_type, unit_number = classification
        heading = _annex_heading(div) if unit_type == "annex" else _heading(div)
        chapter = _enclosing(div, "cpt")
        section = _enclosing(div, "sct")
        unit_id = str(div.get("id") or "")

        for index, (text, ancestor_chain) in enumerate(_walk_paragraphs(div, [])):
            yield Paragraph(
                source=source,
                celex=celex,
                eli=eli,
                unit_type=unit_type,
                unit_id=unit_id,
                unit_number=unit_number,
                chapter=chapter,
                section=section,
                heading=heading,
                paragraph_index=index,
                text=text,
                parent_text="\n".join(ancestor_chain) if ancestor_chain else None,
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
    paragraphs = list(chunk_xhtml(path.read_text(encoding="utf-8"), key, meta["celex"], meta["eli"]))

    out_path = path.parent / f"{path.stem}.chunks.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for p in paragraphs:
            f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")

    by_type: dict[str, int] = {}
    units: dict[tuple[str, str], int] = {}
    for p in paragraphs:
        by_type[p.unit_type] = by_type.get(p.unit_type, 0) + 1
        units[(p.unit_type, p.unit_id)] = 1
    unit_count = len(units)
    summary = ", ".join(f"{t}={n}" for t, n in sorted(by_type.items()))
    print(f"{path.name}: {len(paragraphs)} paragraphs across {unit_count} units ({summary}) -> {out_path}")
    return out_path


def main(argv: list[str]) -> None:
    paths = [Path(p) for p in argv] if argv else DEFAULT_PATHS
    for p in paths:
        process(p)


if __name__ == "__main__":
    main(sys.argv[1:])
