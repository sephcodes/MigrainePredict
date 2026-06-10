"""
EUR-Lex XHTML loader for the AI Act and GDPR.

Parses EUR-Lex's ELI-marked XHTML manifestation into structured chunks
suitable for downstream deontic extraction.

Run:
    python eurlex_loader.py /path/to/gdpr.html
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator, Literal

from bs4 import BeautifulSoup, Tag

UnitType = Literal["recital", "article", "citation", "annex", "final_provision"]


@dataclass
class RegulatoryUnit:
    """A single addressable unit from a regulation: an article, recital, etc."""
    source_celex: str            # e.g. "32016R0679"
    source_eli: str              # canonical ELI URI
    unit_type: UnitType          # recital | article | citation | annex | final_provision
    unit_id: str                 # e.g. "art_32", "rct_71"
    unit_number: str | None      # e.g. "32", "71"
    chapter: str | None = None   # e.g. "IV"
    section: str | None = None   # e.g. "2"
    heading: str | None = None   # e.g. "Security of processing"
    text: str = ""               # full plain text of the unit
    paragraphs: list[str] = field(default_factory=list)


# ELI markup conventions discovered from EUR-Lex XHTML.
_ID_PATTERNS = {
    "recital":         re.compile(r"^rct_(\d+)$"),
    "article":         re.compile(r"^art_(\d+)$"),
    "citation":        re.compile(r"^cit_(\d+)$"),
    "annex":           re.compile(r"^anx_([IVX]+|\d+)$"),
    "final_provision": re.compile(r"^fnp_(\d+)$"),
}


def _classify_unit(div: Tag) -> tuple[UnitType, str] | None:
    """Return (unit_type, unit_number) if div is a leaf regulatory unit, else None."""
    div_id = div.get("id", "")
    for unit_type, pattern in _ID_PATTERNS.items():
        m = pattern.match(div_id)
        if m:
            return unit_type, m.group(1)  # type: ignore[return-value]
    return None


def _extract_text(div: Tag) -> tuple[str, list[str]]:
    """Extract clean text from a unit, returning (joined_text, paragraph_list)."""
    paragraphs = []
    for p in div.find_all("p", class_="oj-normal"):
        text = " ".join(p.get_text(" ", strip=True).split())
        if text and not re.fullmatch(r"\(\d+\)", text):  # skip standalone numbering
            paragraphs.append(text)
    return "\n\n".join(paragraphs), paragraphs


def _find_enclosing_label(div: Tag, prefix: str) -> str | None:
    """Walk up the DOM to find the chapter/section this unit sits within."""
    current = div.parent
    while current is not None and current.name:
        cid = current.get("id", "") if isinstance(current, Tag) else ""
        # IDs look like 'cpt_IV' or 'cpt_IV.sct_2'; we extract the last matching segment
        if isinstance(cid, str) and cid:
            for segment in cid.split("."):
                if segment.startswith(f"{prefix}_"):
                    return segment[len(prefix) + 1:]
        current = current.parent if isinstance(current, Tag) else None
    return None


def _find_heading(div: Tag) -> str | None:
    """Article and chapter divs have a title paragraph; pull it."""
    for cls in ("oj-sti-art", "oj-ti-art", "oj-sti-chp", "oj-ti-chp"):
        node = div.find("p", class_=cls)
        if node:
            return " ".join(node.get_text(" ", strip=True).split())
    # Fallback: first short bold-ish paragraph that isn't the article number
    return None


def parse_eurlex_html(
    html: str,
    source_celex: str,
    source_eli: str,
) -> Iterator[RegulatoryUnit]:
    """Yield RegulatoryUnits from an EUR-Lex XHTML document."""
    soup = BeautifulSoup(html, "html.parser")

    # Strip the navigation sidebar so we don't double-count TOC links.
    for nav in soup.find_all("nav"):
        nav.decompose()
    for toc in soup.find_all(id="TOC"):
        toc.decompose()

    for div in soup.find_all("div", class_="eli-subdivision"):
        classification = _classify_unit(div)
        if classification is None:
            continue
        unit_type, unit_number = classification

        text, paragraphs = _extract_text(div)
        if not text:
            continue

        yield RegulatoryUnit(
            source_celex=source_celex,
            source_eli=source_eli,
            unit_type=unit_type,
            unit_id=div.get("id", ""),
            unit_number=unit_number,
            chapter=_find_enclosing_label(div, "cpt"),
            section=_find_enclosing_label(div, "sct"),
            heading=_find_heading(div),
            text=text,
            paragraphs=paragraphs,
        )


# Source registry for the two target regulations.
SOURCES = {
    "gdpr": {
        "celex": "32016R0679",
        "eli": "http://data.europa.eu/eli/reg/2016/679/oj",
    },
    "aiact": {
        "celex": "32024R1689",
        "eli": "http://data.europa.eu/eli/reg/2024/1689/oj",
    },
}


def main(path: str) -> None:
    html = Path(path).read_text(encoding="utf-8")
    name = "gdpr" if "2016" in path else "aiact"
    src = SOURCES[name]
    units = list(parse_eurlex_html(html, src["celex"], src["eli"]))

    # Summary stats.
    by_type: dict[str, int] = {}
    for u in units:
        by_type[u.unit_type] = by_type.get(u.unit_type, 0) + 1

    print(f"Source: {src['celex']} ({src['eli']})")
    print(f"Total units: {len(units)}")
    for t, n in sorted(by_type.items()):
        print(f"  {t}: {n}")

    # Show a sample article (GDPR Article 32 if present, otherwise the first article).
    sample = next(
        (u for u in units if u.unit_type == "article" and u.unit_number == "32"),
        next((u for u in units if u.unit_type == "article"), None),
    )
    if sample:
        print("\n--- Sample article ---")
        print(f"{sample.unit_id}  chapter={sample.chapter}  section={sample.section}")
        print(f"heading: {sample.heading}")
        print(f"text (first 400 chars):\n{sample.text[:400]}...")


if __name__ == "__main__":
    main(sys.argv[1])
