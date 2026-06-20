"""
Post-screening enrichment for the KEPT paragraphs from each regulation.

For each paragraph that survived the LLM screener this script adds:

  1. `iri` — a canonical paragraph identifier of the form
     `<source>:<unit_id>/p_<paragraph_index>` (e.g., `gdpr:art_5/p_3`,
     `aiact:annex_iii/p_17`). The downstream deontic extractor references
     this verbatim so it can't invent variants.

  2. `previous_sibling` / `next_sibling` — a ±1 window over the same
     `unit_id` (article, recital, annex). Each sibling carries its own
     `iri`, the paragraph text, and the `screen_keep` flag so the
     extractor knows whether the neighbour was deemed substantive. Drops
     are included as context but do not get their own postscreen records.

  3. `cross_references` — regex-extracted candidate REFERS_TO targets
     pulled from the paragraph's own `text` (not its `parent_text`).
     Each entry is `{raw, kind, resolved_iri}`. Within-corpus references
     and known cross-corpus references resolve by matching regulatory
     markers; everything else gets `resolved_iri: null` for the LLM to
     confirm or veto.

Input:  data/{source}.screened.jsonl   (annotate-everything output of screen.py)
Output: data/{source}.postscreened.jsonl   (KEEP-only, enriched)

Usage:
    python postscreen.py
    python postscreen.py data/gdpr.screened.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_PATHS = [
    Path("data/gdpr.screened.jsonl"),
    Path("data/aiact.screened.jsonl"),
]

# Known regulation citations resolvable within our corpus. A cross-regulation
# reference from one of these to the other resolves to a canonical IRI in the
# target corpus; references to anything else get `resolved_iri = null`.
KNOWN_REGULATIONS: dict[str, str] = {
    "Regulation (EU) 2016/679":  "gdpr",
    "Regulation (EU) 2024/1689": "aiact",
}

# Order matters: longer numerals first so the regex prefers "(viii)" over "(v)".
_ROMAN_ALT = r"(?:xviii|xvii|xvi|xv|xiv|xiii|xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i)"

# Permissive cross-regulation citation forms accepted in the optional
# "Article N of …" tail and the bare-cite regexes below.
_REGULATION_GROUP = (
    r"Regulation\s+\((?:EU|EC|EEC)\)(?:\s+No)?\s+\d+/\d+"
    r"|Directive\s+(?:\((?:EU|EC|EEC)\)\s+)?\d+/\d+/\w+"
    r"|Decision\s+(?:\((?:EU|EC|EEC)\)\s+)?(?:No\s+)?\d+/\d+(?:/\w+)?"
)


# ---------------------------------------------------------------------------
# IRI minting & marker parsing
# ---------------------------------------------------------------------------

def _marker_segment(marker: str | None) -> str | None:
    """Turn a leading marker into an IRI segment.

    Paragraph-level markers (`N.` and `(N)` numeric parens) → `par_<n>`. The
    `(N)` case catches GDPR Art 4's definitions where numbered items appear in
    parens at the top level rather than as `N.` lead-ins. Point-level markers
    (lettered or roman parens) → `pt_<x>`."""
    if not marker:
        return None
    if marker.endswith("."):
        return f"par_{marker[:-1]}"
    if marker.startswith("(") and marker.endswith(")"):
        inner = marker[1:-1]
        if inner.isdigit():
            return f"par_{inner}"
        return f"pt_{inner}"
    return None


def _is_paragraph_marker(marker: str | None) -> bool:
    """True if `marker` is a paragraph-level marker (`N.` or `(N)` numeric)."""
    if not marker:
        return False
    return bool(re.fullmatch(r"\d+\.", marker) or re.fullmatch(r"\(\d+\)", marker))


def _clean_heading(heading: str | None) -> str | None:
    """Strip trailing backticks and whitespace that occasionally appear as
    EUR-Lex source-HTML typos (e.g., AI Act Art 1's 'Subject matter`')."""
    if not heading:
        return heading
    return re.sub(r"`+\s*$", "", heading).strip() or None


def leading_marker(text: str | None) -> str | None:
    """Return a paragraph's leading regulatory marker as a canonical string
    (e.g. '1.', '(1)', '(a)', '(i)'), or None if no recognisable marker is
    present.

    Romans are tried before letters so `(i)`, `(v)`, `(x)` are classified
    consistently regardless of whether the surrounding list is alphabetic or
    roman — both forms canonicalise to the same string for matching, which is
    what cross-reference resolution wants.

    Numbered parenthesised markers like `(16)` are recognised so GDPR Art 4
    definitions (and any other article that uses `(N)` instead of `N.` for its
    top-level enumeration) get a stable IRI segment. The recital opener `(26)`
    looks identical syntactically, so the recital case is handled separately
    in `paragraph_iri`."""
    if not text:
        return None
    t = text.lstrip()
    m = re.match(r"^(\d+)\.(?=\s|$)", t)
    if m:
        return f"{m.group(1)}."
    m = re.match(rf"^\(({_ROMAN_ALT})\)(?=\s|$)", t)
    if m:
        return f"({m.group(1)})"
    m = re.match(r"^\((\d+)\)(?=\s|$)", t)
    if m:
        return f"({m.group(1)})"
    m = re.match(r"^\(([a-z])\)(?=\s|$)", t)
    if m:
        return f"({m.group(1)})"
    return None


def parent_marker_chain(parent_text: str | None) -> list[str]:
    """Extract the chain of ancestor markers from a paragraph's parent_text
    (a newline-joined string of ancestor lead-ins)."""
    if not parent_text:
        return []
    out: list[str] = []
    for line in parent_text.split("\n"):
        m = leading_marker(line)
        if m:
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# Paragraph index & lookup
# ---------------------------------------------------------------------------

def build_paragraph_index(records: list[dict]) -> dict[tuple[str, int], dict]:
    return {(r["unit_id"], r["paragraph_index"]): r for r in records}


def build_text_index(records: list[dict]) -> dict[tuple[str, str], int]:
    """`(unit_id, text) → paragraph_index` reverse map. Used to mint IRIs for
    each line of a paragraph's parent chain by matching the line text back to
    the ancestor paragraph it came from."""
    return {(r["unit_id"], r["text"]): r["paragraph_index"] for r in records}


def build_iri_maps(records: list[dict], source: str
                   ) -> tuple[dict[tuple[str, int], str], dict[str, dict]]:
    """Compute every paragraph's hierarchical IRI up front, returning two
    lookup tables: `(unit_id, paragraph_index) → iri` for forward lookups,
    and `iri → record` for reverse lookups (used by the text-fill step).

    Three structural rules drive the per-unit pre-pass:

      1. *Recitals* get `<reg>:<unit_id>/p_<paragraph_index>` directly; the
         opening `(N)` is the recital number, not a sub-item marker.

      2. *Implicit par_1*: a unit without any paragraph-level marker
         (`N.` or `(N)` numeric) is treated as having one implicit numbered
         paragraph. The unmarked chapeau (Art 10's single body paragraph,
         Art 50's `"In relation to third countries…"`) becomes `par_1`, and
         lettered sub-items get `par_1` prepended (Art 50 (b) →
         `par_1/pt_b`).

      3. *Continuations*: an unmarked paragraph with marker-based ancestors
         (explicit `parent_text` or a walk-back hit to the most recent
         paragraph-level-marked paragraph) becomes `<ancestor>/cont_<N>`,
         where `N` is a 1-indexed counter within the parent group. Covers
         Art 6 p_7 ("Point (f) of the first subparagraph…") and Art 53(1)'s
         em-dash bullets uniformly.

    Some units (AI Act Annex VIII with its Section A/B/C blocks) still have
    repeating marker patterns the chunker can't disambiguate via parent_text.
    If the tentative hierarchical IRIs collide within a unit, every paragraph
    in that unit falls back to flat `<reg>:<unit_id>/p_<paragraph_index>`."""
    by_unit: dict[str, list[dict]] = {}
    for rec in records:
        by_unit.setdefault(rec["unit_id"], []).append(rec)
    for paras in by_unit.values():
        paras.sort(key=lambda r: r["paragraph_index"])

    text_idx = build_text_index(records)
    tentative: dict[tuple[str, int], str] = {}

    def _ancestor_segs_from_parent(unit_id: str, parent_text: str | None,
                                   has_para_markers: bool) -> list[str]:
        """Compute the ancestor-segment list for a paragraph by looking up the
        immediate parent record (last line of `parent_text`) and reusing the
        IRI segments we already minted for it. This naturally cascades
        through deeper ancestors and correctly anchors sub-items whose
        immediate parent is itself a `cont_N` continuation paragraph.

        Chapeau lead-ins (parent IRIs ending in `/p_<n>`) are *skipped*: in
        legal drafting the chapeau and its enumerated items live at the same
        hierarchical level, so the chapeau is not a parent in the IRI sense.
        Without this rule Art 4's `"For the purposes of this Regulation:"`
        would push its definitions into `art_4/p_0/par_1` etc."""
        if not parent_text:
            return ["par_1"] if not has_para_markers else []
        prefix = f"{source}:{unit_id}/"
        for line in reversed(parent_text.split("\n")):
            parent_pi = text_idx.get((unit_id, line))
            if parent_pi is None:
                continue
            parent_iri = tentative.get((unit_id, parent_pi))
            if not (parent_iri and parent_iri.startswith(prefix)):
                continue
            segs = parent_iri[len(prefix):].split("/")
            if segs and re.fullmatch(r"p_\d+", segs[-1]):
                continue  # chapeau lead-in — skip and try further up
            if not has_para_markers and segs and segs[0] != "par_1":
                segs = ["par_1"] + segs
            return segs
        # No parent IRI found — fall back to parsing markers off the chain
        # and prepending implicit par_1 if appropriate.
        ancestor_markers = parent_marker_chain(parent_text)
        segs = [s for s in (_marker_segment(m) for m in ancestor_markers) if s]
        if not has_para_markers and (not segs or segs[0] != "par_1"):
            segs = ["par_1"] + segs
        return segs

    for unit_id, paras in by_unit.items():
        has_para_markers = any(
            _is_paragraph_marker(leading_marker(r["text"])) for r in paras
        )
        recent_para_pi: int | None = None
        cont_counters: dict[tuple, int] = {}

        for rec in paras:
            pi = rec["paragraph_index"]

            if rec["unit_type"] == "recital":
                tentative[(unit_id, pi)] = f"{source}:{unit_id}/p_{pi}"
                continue

            own_marker = leading_marker(rec["text"])

            if own_marker and _is_paragraph_marker(own_marker):
                recent_para_pi = pi

            ancestor_segs = _ancestor_segs_from_parent(
                unit_id, rec.get("parent_text"), has_para_markers
            )

            own_seg = _marker_segment(own_marker)

            if own_seg:
                segments = ancestor_segs + [own_seg]
            elif (not has_para_markers
                  and not rec.get("parent_text")
                  and ancestor_segs == ["par_1"]):
                # The paragraph IS the implicit par_1 (single-paragraph
                # article body, or chapeau of a points-only article).
                segments = ["par_1"]
            elif rec.get("parent_text") and ancestor_segs:
                # Unmarked continuation with an explicit parent.
                key = tuple(ancestor_segs)
                cont_counters[key] = cont_counters.get(key, 0) + 1
                segments = ancestor_segs + [f"cont_{cont_counters[key]}"]
            elif recent_para_pi is not None and rec["unit_type"] == "article":
                # Walk back to the most recent paragraph-level-marked paragraph
                # and attach as a continuation. Articles only — for annexes,
                # repeating marker patterns across sections (Annex VIII A/B/C)
                # would create false attachments and suppress the collision
                # fallback that gives those units a consistent flat scheme.
                prev_rec = next(p for p in paras if p["paragraph_index"] == recent_para_pi)
                prev_seg = _marker_segment(leading_marker(prev_rec["text"]))
                key = (prev_seg,)
                cont_counters[key] = cont_counters.get(key, 0) + 1
                segments = [prev_seg, f"cont_{cont_counters[key]}"]
            else:
                # Top-level unmarked, nothing to attach to.
                segments = [f"p_{pi}"]

            tentative[(unit_id, pi)] = f"{source}:{unit_id}/" + "/".join(segments)

    iri_count: dict[str, int] = {}
    for iri in tentative.values():
        iri_count[iri] = iri_count.get(iri, 0) + 1
    units_with_collision = {
        uid for (uid, _), iri in tentative.items() if iri_count[iri] > 1
    }

    iri_by_pos: dict[tuple[str, int], str] = {}
    iri_to_rec: dict[str, dict] = {}
    for rec in records:
        unit_id = rec["unit_id"]
        pi = rec["paragraph_index"]
        iri = (f"{source}:{unit_id}/p_{pi}"
               if unit_id in units_with_collision
               else tentative[(unit_id, pi)])
        iri_by_pos[(unit_id, pi)] = iri
        iri_to_rec[iri] = rec
    return iri_by_pos, iri_to_rec


def parent_chain(parent_text: str | None, unit_id: str,
                 text_idx: dict[tuple[str, str], int],
                 iri_by_pos: dict[tuple[str, int], str],
                 own_iri: str,
                 iri_to_rec: dict[str, dict]) -> list[dict] | None:
    """Convert the newline-joined `parent_text` string into a list of
    `{iri, text}` entries, one per ancestor in chain order.

    For walk-back continuations whose `parent_text` is None but whose IRI
    ends with `/cont_<N>` (e.g., `gdpr:art_6/par_1/cont_1`), the immediate
    parent is recovered by stripping that suffix and looking up the resulting
    IRI in the corpus. Without this the `parent` field would be `None` for
    every walk-back continuation even though its IRI implies containment."""
    if parent_text:
        out: list[dict] = []
        for line in parent_text.split("\n"):
            pi = text_idx.get((unit_id, line))
            iri = iri_by_pos.get((unit_id, pi)) if pi is not None else None
            out.append({"iri": iri, "text": line})
        return out
    # Walk-back continuation: parent IRI is the own IRI minus its `/cont_<N>` tail.
    if own_iri and "/cont_" in own_iri:
        parent_iri = own_iri.rsplit("/cont_", 1)[0]
        parent_rec = iri_to_rec.get(parent_iri)
        if parent_rec is not None:
            return [{"iri": parent_iri, "text": parent_rec["text"]}]
    return None


def sibling_record(by_pos: dict, iri_by_pos: dict[tuple[str, int], str],
                   unit_id: str, paragraph_index: int, offset: int) -> dict | None:
    target = paragraph_index + offset
    if target < 0:
        return None
    rec = by_pos.get((unit_id, target))
    if rec is None:
        return None
    return {
        "iri": iri_by_pos[(unit_id, target)],
        "text": rec["text"],
        "screen_keep": rec["screen_keep"],
    }


def unit_exists(by_pos: dict, unit_id: str) -> bool:
    return any(uid == unit_id for uid, _ in by_pos.keys())


def find_paragraph_by_markers(by_pos: dict, unit_id: str,
                              target_markers: list[str]) -> int | None:
    """Find paragraph_index of a paragraph in `unit_id` whose leading marker
    equals `target_markers[-1]` and whose parent-marker chain ENDS with
    `target_markers[:-1]`. Suffix-match (not strict prefix) so a target like
    `("(h)", "(i)")` matches a paragraph with chain `["1.", "(h)"]`."""
    if not target_markers:
        return None
    last = target_markers[-1]
    ancestors = target_markers[:-1]
    candidates = sorted(
        (pi, rec) for (uid, pi), rec in by_pos.items() if uid == unit_id
    )
    for pi, rec in candidates:
        if leading_marker(rec["text"]) != last:
            continue
        if ancestors:
            chain = parent_marker_chain(rec.get("parent_text"))
            if chain[-len(ancestors):] != ancestors:
                continue
        return pi
    return None


def _resolve_with_fallback(by_pos: dict, unit_id: str,
                           target_markers: list[str]) -> int | None:
    """Try `target_markers` as-is, then fall back through two retries.

    The `N.`-↔-`(N)` retry catches Art 4's `(16)`-style numbering (where the
    parser builds `["16.", "(a)"]` but the actual paragraph uses `(16)`).

    The implicit-par_1 retry catches `paragraph 1` and `Article N(1)(letter)`
    references into units that have no paragraph-level marker (Art 10, Art 50,
    etc.). For a single-marker target the chapeau (first paragraph) is
    returned; for a multi-marker target the leading `1.` / `(1)` is dropped
    and the remainder is resolved against the unit's points directly."""
    idx = find_paragraph_by_markers(by_pos, unit_id, target_markers)
    if idx is not None or not target_markers:
        return idx
    first = target_markers[0]
    if re.fullmatch(r"\d+\.", first):
        alt = [f"({first[:-1]})"] + target_markers[1:]
        idx = find_paragraph_by_markers(by_pos, unit_id, alt)
        if idx is not None:
            return idx
    if first in ("1.", "(1)"):
        has_para_markers = any(
            _is_paragraph_marker(leading_marker(rec["text"]))
            for (uid, _), rec in by_pos.items()
            if uid == unit_id
        )
        if not has_para_markers:
            if len(target_markers) == 1:
                first_pi = sorted(pi for (uid, pi) in by_pos.keys() if uid == unit_id)
                return first_pi[0] if first_pi else None
            return find_paragraph_by_markers(by_pos, unit_id, target_markers[1:])
    return None


# ---------------------------------------------------------------------------
# Cross-reference patterns
# ---------------------------------------------------------------------------

RE_ARTICLE = re.compile(
    r"\bArticles?\s+"
    r"(\d+)(\(\d+\))?(\([a-z]\))?(\((?:" + _ROMAN_ALT + r")\))?"
    r"((?:\s*(?:,|and|or|to)\s*\d+)*)"            # g5: chained/range article numbers
    r"(?:\s+of\s+(" + _REGULATION_GROUP + r"))?"  # g6: of Regulation
)

RE_ANNEX = re.compile(
    r"\bAnnex(?:es)?\s+([IVXLCDM]+|\d+)"
    r"(?:\s+of\s+(" + _REGULATION_GROUP + r"))?"
)

RE_PARAGRAPH = re.compile(
    r"\b[Pp]aragraphs?\s+(\d+)"
    r"((?:\s*(?:,|and|or|to)\s*\d+)*)"   # g2: chained/range paragraph numbers
)
# Match a point lead-in and any chained additional letters (", (X)", " and (X)",
# " or (X)"). Without the chain group, "points (a) and (b)" emits only pt_a and
# downstream extractors can't reach pt_b. The third group captures the whole
# trailer so we can re-scan it for letters.
RE_POINT = re.compile(
    r"\b[Pp]oints?\s+"
    r"(\([a-z]\))(\((?:" + _ROMAN_ALT + r")\))?"
    r"((?:\s*(?:,|and|or)\s*\([a-z]\))*)"
    r"(?:\s+of\s+Article\s+(\d+)(\(\d+\))?)?"   # g4: of-Article number, g5: of-Article paragraph
)
RE_REGULATION_BARE = re.compile(
    r"\bRegulation\s+\((?:EU|EC|EEC)\)(?:\s+No)?\s+(\d+/\d+)"
)
RE_DIRECTIVE_BARE = re.compile(
    r"\bDirective\s+(?:\((?:EU|EC|EEC)\)\s+)?(\d+/\d+/\w+)"
)
RE_STRUCTURAL = re.compile(r"\b(Chapter|Section|Title)\s+([IVXLCDM]+|\d+)")


def _normalize_regulation_cite(s: str) -> str:
    """Collapse whitespace and strip an internal 'No ' so
    'Regulation (EU) No 2016/679' canonicalises to 'Regulation (EU) 2016/679'
    for KNOWN_REGULATIONS lookup."""
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\bNo\s+", "", s)
    return s


# ---------------------------------------------------------------------------
# Cross-reference extraction
# ---------------------------------------------------------------------------

def _resolve_text(resolved_iri: str | None, source: str,
                  iri_to_rec_by_source: dict[str, dict[str, dict]]) -> str | None:
    """Return the target paragraph's `text` ONLY if (a) `resolved_iri` points
    to a paragraph in the same corpus and (b) that paragraph was screen-kept.
    Returns None for cross-corpus refs, dropped targets, bare-prefix refs
    (`gdpr:`), bare-unit refs (`gdpr:art_5`), unresolved refs, or anything
    otherwise unavailable."""
    if not resolved_iri or ":" not in resolved_iri:
        return None
    target_source = resolved_iri.split(":", 1)[0]
    if target_source != source:
        return None  # cross-corpus → don't fill
    rec = iri_to_rec_by_source.get(target_source, {}).get(resolved_iri)
    if rec is None or not rec.get("screen_keep"):
        return None  # bare unit, missing, or screen-dropped → don't fill
    return rec["text"]


def extract_cross_references(
        text: str, source: str, current_unit_id: str,
        by_corpus_pos: dict[str, dict],
        iri_by_pos_by_source: dict[str, dict[tuple[str, int], str]],
        iri_to_rec_by_source: dict[str, dict[str, dict]]) -> list[dict]:
    """Walk `text` and return a deduped list of
    `{raw, kind, resolved_iri, text}` candidate REFERS_TO targets. Earlier
    passes (Article, Annex) reserve their character spans so bare-regulation
    matches don't re-emit the regulation suffix already attached to an
    Article ref. `text` is filled only when the resolved target is in the
    same corpus and was screen-kept."""
    refs: list[dict] = []
    seen_raw: set[str] = set()
    consumed_spans: list[tuple[int, int]] = []

    def is_consumed(span: tuple[int, int]) -> bool:
        s, e = span
        return any(cs <= s and e <= ce for cs, ce in consumed_spans)

    def emit(raw: str, kind: str, resolved_iri: str | None,
             span: tuple[int, int], surface: str | None = None) -> None:
        if raw in seen_raw or is_consumed(span):
            return
        seen_raw.add(raw)
        refs.append({
            "raw": raw,
            "kind": kind,
            "resolved_iri": resolved_iri,
            # The original citation string this IRI came from. For chained/range
            # members (art_16 from "15 to 22") it is the PARENT surface, so the
            # downstream attribution check keys on the text actually present in
            # the regulation, not a synthesised "Article 16".
            "citation_surface": surface if surface is not None else raw,
            "text": _resolve_text(resolved_iri, source, iri_to_rec_by_source),
        })
        consumed_spans.append(span)

    # Article references — may consume an "of Regulation (EU) X/Y" tail.
    for m in RE_ARTICLE.finditer(text):
        raw = m.group(0)
        article_num = m.group(1)
        p_num = m.group(2)[1:-1] if m.group(2) else None
        point = m.group(3)[1:-1] if m.group(3) else None
        sub   = m.group(4)[1:-1] if m.group(4) else None
        reg_cite = m.group(6)

        target_source = source
        if reg_cite:
            target_source = KNOWN_REGULATIONS.get(_normalize_regulation_cite(reg_cite))
            if target_source is None:
                emit(raw, "article", None, m.span())
                continue

        target_pos = by_corpus_pos.get(target_source)
        target_unit = f"art_{article_num}"
        if target_pos is None or not unit_exists(target_pos, target_unit):
            emit(raw, "article", None, m.span())
            continue

        markers: list[str] = []
        if p_num is not None:
            markers.append(f"{p_num}.")
        if point is not None:
            markers.append(f"({point})")
        if sub is not None:
            markers.append(f"({sub})")

        if not markers:
            # Bare "Article N" → the first paragraph IRI of the target unit.
            # Pointing at the unit-base IRI (`gdpr:art_5`) would never match a
            # record because every paragraph has a path beyond the unit.
            first_iri = iri_by_pos_by_source.get(target_source, {}).get((target_unit, 0))
            emit(raw, "article", first_iri, m.span())
        else:
            para_idx = _resolve_with_fallback(target_pos, target_unit, markers)
            if para_idx is None:
                emit(raw, "article", None, m.span())
            else:
                resolved = iri_by_pos_by_source[target_source].get((target_unit, para_idx))
                emit(raw, "article", resolved, m.span())

        # Chained / range article numbers (issue #1): "Articles 13 and 14",
        # "Articles 15 to 22 and 34". Each additional number is a bare article
        # reference (first paragraph IRI). Append directly — the chained numbers
        # share the match span (already consumed), so emit()'s is_consumed check
        # would drop them; seen_raw still dedups.
        prev = int(article_num)
        for conn, num_s in re.findall(r"(,|and|or|to)\s*(\d+)", m.group(5) or ""):
            num = int(num_s)
            nums = range(prev + 1, num + 1) if conn == "to" else [num]
            for n in nums:
                ch_raw = f"Article {n}"
                if ch_raw in seen_raw:
                    continue
                ch_unit = f"art_{n}"
                ch_iri = (iri_by_pos_by_source.get(target_source, {}).get((ch_unit, 0))
                          if (target_pos is not None and unit_exists(target_pos, ch_unit))
                          else None)
                seen_raw.add(ch_raw)
                refs.append({"raw": ch_raw, "kind": "article", "resolved_iri": ch_iri,
                             "citation_surface": raw,  # parent surface, e.g. "Articles 15 to 22 and 34"
                             "text": _resolve_text(ch_iri, source, iri_to_rec_by_source)})
            prev = num

    # Annex references.
    for m in RE_ANNEX.finditer(text):
        raw = m.group(0)
        annex_id = m.group(1)
        reg_cite = m.group(2)

        target_source = source
        if reg_cite:
            target_source = KNOWN_REGULATIONS.get(_normalize_regulation_cite(reg_cite))
            if target_source is None:
                emit(raw, "annex", None, m.span())
                continue

        target_pos = by_corpus_pos.get(target_source)
        target_unit = f"anx_{annex_id}"
        if target_pos is None or not unit_exists(target_pos, target_unit):
            emit(raw, "annex", None, m.span())
            continue
        # Bare "Annex N" → first paragraph IRI of the annex (same reason as
        # the bare-Article case above).
        first_iri = iri_by_pos_by_source.get(target_source, {}).get((target_unit, 0))
        emit(raw, "annex", first_iri, m.span())

    # Internal "paragraph N" — relative to current article.
    target_pos = by_corpus_pos.get(source)
    iri_by_pos_here = iri_by_pos_by_source.get(source, {})
    for m in RE_PARAGRAPH.finditer(text):
        if is_consumed(m.span()):
            continue
        para_num = m.group(1)
        raw = m.group(0)

        def _resolve_par(n: str):
            i = (_resolve_with_fallback(target_pos, current_unit_id, [f"{n}."])
                 if target_pos is not None else None)
            return iri_by_pos_here.get((current_unit_id, i)) if i is not None else None

        emit(raw, "paragraph", _resolve_par(para_num), m.span())

        # Chained / range paragraph numbers (issue: "paragraphs 1 and 2",
        # "paragraphs 1 to 3"). Each rides on the parent surface so the
        # attribution check keys on the text actually present ("paragraphs 1
        # and 2"). Append directly — chained numbers share the consumed span.
        prev = int(para_num)
        for conn, num_s in re.findall(r"(,|and|or|to)\s*(\d+)", m.group(2) or ""):
            num = int(num_s)
            nums = range(prev + 1, num + 1) if conn == "to" else [num]
            for n in nums:
                ch_raw = f"paragraph {n}"
                if ch_raw in seen_raw:
                    continue
                seen_raw.add(ch_raw)
                ch_iri = _resolve_par(str(n))
                refs.append({"raw": ch_raw, "kind": "paragraph", "resolved_iri": ch_iri,
                             "citation_surface": raw,  # parent surface, e.g. "Paragraphs 1 and 2"
                             "text": _resolve_text(ch_iri, source, iri_to_rec_by_source)})
            prev = num

    # Internal "point (X)" — relative to current article. The trailer group
    # captures chained additional letters ("(a) and (b)", "(a), (b) and (c)");
    # each chained letter gets emitted as its own point reference so
    # downstream extractors see every cited sub-item.
    for m in RE_POINT.finditer(text):
        if is_consumed(m.span()):
            continue
        point_marker = m.group(1)
        sub_marker = m.group(2)
        trailer = m.group(3) or ""
        of_art = m.group(4)           # "point (e) of Article 6(1)" -> "6"
        of_par = m.group(5)           # -> "(1)"
        raw = m.group(0)

        # Issue #2: "point (X) of Article N" resolves the point against the
        # CITED article (with its paragraph), not the current one. A plain
        # "point (X)" stays relative to the current article (unchanged).
        if of_art and target_pos is not None and unit_exists(target_pos, f"art_{of_art}"):
            pt_unit = f"art_{of_art}"
            base = [f"{of_par[1:-1]}."] if of_par else []
            raw_suffix = f" of Article {of_art}"
            def _resolve_point(mk):
                i = _resolve_with_fallback(target_pos, pt_unit, base + mk)
                return iri_by_pos_here.get((pt_unit, i)) if i is not None else None
        else:
            raw_suffix = ""
            def _resolve_point(mk):
                i = (find_paragraph_by_markers(target_pos, current_unit_id, mk)
                     if target_pos is not None else None)
                return iri_by_pos_here.get((current_unit_id, i)) if i is not None else None

        markers = [point_marker] + ([sub_marker] if sub_marker else [])
        emit(raw, "point", _resolve_point(markers), m.span())

        for letter in re.findall(r"\(([a-z])\)", trailer):
            chained_marker = f"({letter})"
            chained_raw = f"point {chained_marker}{raw_suffix}"
            if chained_raw in seen_raw:
                continue
            # Bypass the is_consumed check — the chained marker shares the
            # parent match's span, already consumed by the primary emit.
            seen_raw.add(chained_raw)
            chained_resolved = _resolve_point([chained_marker])
            refs.append({
                "raw": chained_raw,
                "kind": "point",
                "resolved_iri": chained_resolved,
                "citation_surface": raw,  # parent surface, e.g. "point (e) or (f) of Article 6(1)"
                "text": _resolve_text(chained_resolved, source, iri_to_rec_by_source),
            })

    # Bare regulation cites — skip spans already consumed by Article/Annex refs.
    for m in RE_REGULATION_BARE.finditer(text):
        if is_consumed(m.span()):
            continue
        raw = m.group(0)
        prefix = KNOWN_REGULATIONS.get(_normalize_regulation_cite(raw))
        resolved = f"{prefix}:" if prefix else None
        emit(raw, "regulation", resolved, m.span())

    for m in RE_DIRECTIVE_BARE.finditer(text):
        if is_consumed(m.span()):
            continue
        emit(m.group(0), "directive", None, m.span())

    for m in RE_STRUCTURAL.finditer(text):
        if is_consumed(m.span()):
            continue
        emit(m.group(0), m.group(1).lower(), None, m.span())

    # Drop a bare-article ref that a point ref from the same citation makes
    # redundant (e.g. art_6/par_1 alongside art_6/par_1/pt_e from "point (e) of
    # Article 6(1)" — the specific point is what was cited, not the paragraph).
    point_iris = {r["resolved_iri"] for r in refs
                  if r["kind"] == "point" and r.get("resolved_iri")}
    refs = [r for r in refs if not (
        r["kind"] == "article" and r.get("resolved_iri")
        and any(p.startswith(r["resolved_iri"] + "/") for p in point_iris))]

    return refs


# ---------------------------------------------------------------------------
# Enrichment + main
# ---------------------------------------------------------------------------

def enrich(rec: dict, by_corpus_pos: dict[str, dict],
           text_idx_by_source: dict[str, dict[tuple[str, str], int]],
           iri_by_pos_by_source: dict[str, dict[tuple[str, int], str]],
           iri_to_rec_by_source: dict[str, dict[str, dict]]) -> dict:
    source = rec["source"]
    by_pos = by_corpus_pos[source]
    text_idx = text_idx_by_source[source]
    iri_by_pos = iri_by_pos_by_source[source]
    iri_to_rec = iri_to_rec_by_source[source]
    unit_id = rec["unit_id"]
    pi = rec["paragraph_index"]
    own_iri = iri_by_pos[(unit_id, pi)]

    parent = parent_chain(rec.get("parent_text"), unit_id, text_idx,
                          iri_by_pos, own_iri, iri_to_rec)
    # Enrich each parent entry with resolved cross-references found in the
    # ancestor's own text. This exposes parent-rule IRIs (e.g. "Paragraph 1
    # shall not apply" in an Art 9(2) chapeau resolves to gdpr:art_9/par_1)
    # for the downstream extractor's exemption-references rule.
    if parent:
        for entry in parent:
            ent_refs = extract_cross_references(
                entry["text"], source, unit_id, by_corpus_pos,
                iri_by_pos_by_source, iri_to_rec_by_source,
            )
            entry["references"] = [
                r["resolved_iri"] for r in ent_refs if r.get("resolved_iri")
            ]
    new_heading = _clean_heading(rec.get("heading"))

    # Preserve input field order, but clean `heading` and replace `parent_text`
    # in-place with the structured `parent` chain.
    out: dict = {}
    for k, v in rec.items():
        if k == "heading":
            out[k] = new_heading
        elif k == "parent_text":
            out["parent"] = parent
        else:
            out[k] = v

    out["iri"] = iri_by_pos[(unit_id, pi)]
    out["previous_sibling"] = sibling_record(by_pos, iri_by_pos, unit_id, pi, -1)
    out["next_sibling"] = sibling_record(by_pos, iri_by_pos, unit_id, pi, +1)
    out["cross_references"] = extract_cross_references(
        rec["text"], source, unit_id, by_corpus_pos,
        iri_by_pos_by_source, iri_to_rec_by_source,
    )
    return out


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="*",
                   help="Screened JSONL files; defaults to both data/ screened files.")
    args = p.parse_args(argv)

    paths = [Path(x) for x in args.paths] if args.paths else DEFAULT_PATHS

    # Load every corpus first — cross-corpus reference resolution needs the
    # target corpus's paragraph index too.
    records_per_path: dict[Path, list[dict]] = {}
    by_corpus_pos: dict[str, dict[tuple[str, int], dict]] = {}
    text_idx_by_source: dict[str, dict[tuple[str, str], int]] = {}
    iri_by_pos_by_source: dict[str, dict[tuple[str, int], str]] = {}
    iri_to_rec_by_source: dict[str, dict[str, dict]] = {}
    for path in paths:
        if not path.exists():
            print(f"warning: {path} not found, skipping")
            continue
        records = [json.loads(line) for line in path.open(encoding="utf-8")]
        if not records:
            continue
        source = records[0]["source"]
        records_per_path[path] = records
        by_corpus_pos[source] = build_paragraph_index(records)
        text_idx_by_source[source] = build_text_index(records)
        iri_by_pos, iri_to_rec = build_iri_maps(records, source)
        iri_by_pos_by_source[source] = iri_by_pos
        iri_to_rec_by_source[source] = iri_to_rec

    for path, records in records_per_path.items():
        out_path = path.with_name(
            path.name.replace(".screened.jsonl", ".postscreened.jsonl")
        )
        kept = 0
        ref_total = 0
        with out_path.open("w", encoding="utf-8") as dst:
            for rec in records:
                if not rec.get("screen_keep"):
                    continue
                enriched = enrich(rec, by_corpus_pos, text_idx_by_source,
                                  iri_by_pos_by_source, iri_to_rec_by_source)
                dst.write(json.dumps(enriched, ensure_ascii=False) + "\n")
                kept += 1
                ref_total += len(enriched["cross_references"])
        print(f"{path.name}: {kept} kept paragraphs, "
              f"{ref_total} cross-refs extracted -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
