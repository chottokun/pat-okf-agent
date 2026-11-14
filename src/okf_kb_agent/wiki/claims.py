"""Deterministic numeric-claim extraction and sampling for OKF wiki audits.

Extracts sentences and Markdown table rows containing numeric tokens from
``concepts/*.md`` (excluding fenced code blocks and YAML frontmatter) without
modifying the knowledge base or emitting verdicts/scores.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter

__all__ = [
    "ClaimRecord",
    "ClaimsResult",
    "extract_claims",
    "extract_claims_from_note",
]

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HR_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")

# Numeric token: integer, decimal, percentage, range (e.g. 10-20 / 10–20),
# scientific notation (e.g. 1.5e-3), or number with attached unit.
_NUM_CORE = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
_RANGE_SEP = r"(?:\s*[–—~〜-]\s*|\.\.)"
_UNIT_SUFFIX = (
    r"(?:\s*%|[A-Za-z°℃℉µμΩ][A-Za-z0-9°℃℉µμΩ/·⋅^_-]*"
    r"|倍|件|回|個|層|年|月|日|時間|秒|分)?"
)
_NUMERIC_TOKEN_RE = re.compile(
    rf"{_NUM_CORE}(?:{_RANGE_SEP}{_NUM_CORE})?{_UNIT_SUFFIX}"
)

# Sentence boundary: Japanese 。/！/？ (with optional closing quote/paren)
# or ASCII ./!/? not followed by a digit (avoids splitting decimals like 3.14).
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[。！？])(?![」』）)])|(?<=[。！？][」』）)])|(?<=[.!?])(?!\d)(?=\s|$)"
)


@dataclass(frozen=True)
class ClaimRecord:
    """A single extracted numeric-claim unit from a concept note."""

    note: str
    line: int
    section: str | None
    text: str
    numbers: list[str]
    sources: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "note": self.note,
            "line": self.line,
            "section": self.section,
            "text": self.text,
            "numbers": list(self.numbers),
            "sources": list(self.sources),
        }


@dataclass
class ClaimsResult:
    """Result container for deterministic claim extraction and sampling."""

    wiki: str
    total_claims: int
    returned: int
    seed: int
    claims: list[ClaimRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wiki": self.wiki,
            "total_claims": self.total_claims,
            "returned": self.returned,
            "seed": self.seed,
            "claims": [c.to_dict() for c in self.claims],
        }


def _normalize_sources(raw_sources: Any) -> list[str]:
    if isinstance(raw_sources, list):
        return [str(s) for s in raw_sources]
    if isinstance(raw_sources, str):
        return [raw_sources]
    return []


def _body_start_line(full_text: str, body: str) -> int:
    if not body:
        return len(full_text.splitlines()) + 1
    prefix = full_text[: len(full_text) - len(body)]
    return prefix.count("\n") + 1


def _split_sentences_with_lines(
    block_lines: list[tuple[int, str]],
) -> list[tuple[int, str]]:
    """Join consecutive prose lines, split into sentences, and map each to its 1-based start line."""
    if not block_lines:
        return []

    joined_parts: list[str] = []
    char_to_line: list[int] = []
    for idx, (lineno, raw_line) in enumerate(block_lines):
        if idx > 0:
            joined_parts.append(" ")
            char_to_line.append(lineno)
        joined_parts.append(raw_line)
        char_to_line.extend([lineno] * len(raw_line))

    joined = "".join(joined_parts)
    if not joined.strip():
        return []

    units: list[tuple[int, str]] = []
    pos = 0
    for match in _SENTENCE_SPLIT_RE.finditer(joined):
        end = match.end()
        segment = joined[pos:end]
        if segment.strip():
            leading_ws = len(segment) - len(segment.lstrip())
            start_idx = min(pos + leading_ws, len(char_to_line) - 1)
            units.append((char_to_line[start_idx], " ".join(segment.split())))
        pos = end

    tail = joined[pos:]
    if tail.strip():
        leading_ws = len(tail) - len(tail.lstrip())
        start_idx = min(pos + leading_ws, len(char_to_line) - 1)
        units.append((char_to_line[start_idx], " ".join(tail.split())))

    return units


def _iter_body_units(
    body: str,
    start_line: int,
) -> list[tuple[int, str | None, str]]:
    """Yield ``(line, section, normalized_text)`` for sentences and table rows."""
    units: list[tuple[int, str | None, str]] = []
    current_section: str | None = None
    fence_char: str | None = None
    fence_len = 0
    paragraph_buf: list[tuple[int, str]] = []

    def flush_paragraph() -> None:
        if not paragraph_buf:
            return
        for lineno, sentence in _split_sentences_with_lines(paragraph_buf):
            units.append((lineno, current_section, sentence))
        paragraph_buf.clear()

    for offset, raw_line in enumerate(body.splitlines()):
        lineno = start_line + offset
        stripped = raw_line.strip()

        fence_match = _FENCE_RE.match(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if fence_char is None:
                flush_paragraph()
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
                fence_len = 0
            continue

        if fence_char is not None:
            continue

        if not stripped or _HR_RE.match(raw_line):
            flush_paragraph()
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            current_section = heading_match.group(1).strip() or None
            continue

        if stripped.startswith("|"):
            flush_paragraph()
            if not _TABLE_SEP_RE.match(stripped):
                units.append((lineno, current_section, " ".join(stripped.split())))
            continue

        if _LIST_ITEM_RE.match(raw_line):
            flush_paragraph()
            paragraph_buf.append((lineno, stripped))
            continue

        paragraph_buf.append((lineno, stripped))

    flush_paragraph()
    return units


def extract_claims_from_note(
    note_path: Path,
    wiki_root: Path,
) -> list[ClaimRecord]:
    """Extract all numeric-claim records from a single concept note."""
    text = note_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    sources = _normalize_sources(meta.get("sources"))
    rel_note = note_path.resolve().relative_to(wiki_root.resolve()).as_posix()
    start_line = _body_start_line(text, body)

    claims: list[ClaimRecord] = []
    for lineno, section, unit_text in _iter_body_units(body, start_line):
        numbers = _NUMERIC_TOKEN_RE.findall(unit_text)
        if not numbers:
            continue
        claims.append(
            ClaimRecord(
                note=rel_note,
                line=lineno,
                section=section,
                text=unit_text,
                numbers=numbers,
                sources=sources,
            )
        )
    return claims


def _matches_note_filter(note_path: Path, rel_note: str, filters: set[str]) -> bool:
    if not filters:
        return True
    return (
        note_path.stem in filters
        or note_path.name in filters
        or rel_note in filters
    )


def extract_claims(
    wiki_root: str | Path,
    *,
    sample: int | None = None,
    seed: int = 0,
    notes: list[str] | None = None,
) -> ClaimsResult:
    """Extract and optionally sample numeric claims from ``concepts/*.md``."""
    if sample is not None and sample < 0:
        raise ValueError("--sample must be non-negative")

    root = Path(wiki_root).resolve()
    concepts_dir = root / "concepts"
    note_filters = {n.strip() for n in (notes or []) if n and n.strip()}

    all_claims: list[ClaimRecord] = []
    if concepts_dir.is_dir():
        for path in sorted(concepts_dir.glob("*.md")):
            if path.name == "index.md" or not path.is_file():
                continue
            rel_note = path.relative_to(root).as_posix()
            if not _matches_note_filter(path, rel_note, note_filters):
                continue
            all_claims.extend(extract_claims_from_note(path, root))

    all_claims.sort(key=lambda c: (c.note, c.line))
    total_claims = len(all_claims)

    if sample is not None:
        selected = list(all_claims)
        random.Random(seed).shuffle(selected)
        selected = selected[:sample]
    else:
        selected = all_claims

    return ClaimsResult(
        wiki=str(root),
        total_claims=total_claims,
        returned=len(selected),
        seed=seed,
        claims=selected,
    )
