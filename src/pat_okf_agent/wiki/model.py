"""OKF concept nodes: frontmatter, Markdown body and relative links."""
from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .frontmatter import FrontmatterError, dump_document, parse_frontmatter

__all__ = [
    "BASIS_VALUES",
    "KNOWN_SOURCE_KINDS",
    "NEWS_SOURCE_KINDS",
    "SCOPE_WEIGHTS",
    "SOURCE_ATTR_KEYS",
    "SourceEntry",
    "TRANSPARENT_HEADINGS",
    "WEB_SOURCE_KINDS",
    "Concept",
    "Link",
    "Section",
    "extract_links",
    "extract_sections",
    "find_wikilinks",
    "is_special",
    "is_transparent_heading",
    "iter_concepts",
    "load_concept",
    "parse_source_entry",
    "relevance",
    "scope_from_heading",
    "scope_of",
    "tokenize",
    "validate_meta",
    "write_concept",
]

SCOPE_WEIGHTS: dict[str, float] = {
    "taxonomy": 1.0,
    "workflow": 1.2,
    "synergistic": 1.5,
    "gotchas": 2.0,
}

BASIS_VALUES: tuple[str, ...] = ("external", "model", "mixed")

KNOWN_SOURCE_KINDS: tuple[str, ...] = (
    "patent-biblio",
    "patent-claims",
    "patent-legal",
    "patent-family",
    "paper",
    "preprint",
    "datasheet",
    "product-page",
    "press-release",
    "standard",
    "news",
    "web",
    "manual",
)

WEB_SOURCE_KINDS: tuple[str, ...] = (
    "product-page",
    "press-release",
    "news",
    "web",
)

NEWS_SOURCE_KINDS: tuple[str, ...] = (
    "press-release",
    "news",
)

SOURCE_ATTR_KEYS: tuple[str, ...] = ("issued", "retrieved", "via")

TRANSPARENT_HEADINGS: tuple[str, ...] = (
    "一般知識（未検証）",
    "general knowledge (unverified)",
    "出典に基づく整理",
    "based on sources",
)

_ISO_DATE_STRICT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_transparent_heading(heading: str) -> bool:
    """Return True when ``heading`` is an epistemic modifier heading."""
    cleaned = heading.strip().casefold()
    return cleaned in TRANSPARENT_HEADINGS


@dataclass(frozen=True)
class SourceEntry:
    kind: str
    id: str
    attrs: dict[str, str]
    raw: str


def _is_valid_iso_date(value: str) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_STRICT_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_source_entry(entry: str) -> SourceEntry:
    """Parse and syntax-check one ``sources`` item (raises ValueError)."""
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError("source entry must be a non-empty string")
    tokens = entry.strip().split()
    head = tokens[0]
    if ":" not in head:
        raise ValueError(f"source entry missing '<kind>:<id>': {entry!r}")
    kind, source_id = head.split(":", 1)
    if not kind or not source_id:
        raise ValueError(
            f"source entry must have non-empty kind and id: {entry!r}")
    attrs: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise ValueError(
                f"invalid source attribute (expected key=value): {token!r}")
        key, val = token.split("=", 1)
        if key not in SOURCE_ATTR_KEYS:
            raise ValueError(f"unknown source attribute key: {key!r}")
        if key in attrs:
            raise ValueError(f"duplicate source attribute key: {key!r}")
        if not val:
            raise ValueError(
                f"empty value for source attribute {key!r}: {entry!r}")
        if key in ("issued", "retrieved") and not _is_valid_iso_date(val):
            raise ValueError(
                f"invalid ISO date for {key}={val!r} in {entry!r}")
        attrs[key] = val
    return SourceEntry(kind=kind, id=source_id, attrs=attrs,
                       raw=entry.strip())

SPECIAL_FILES = {"index.md", "log.md"}
_INDEX_RE = re.compile(r"^index(?:-\d+)?\.md$")

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TOKEN_RE = re.compile(r"[0-9A-Za-z_]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+")
_LINK_RE = re.compile(
    r"(?<!!)\[([^\]]*)\]\(\s*(<[^>]*>|[^)\s]+)"
    r"(?:\s+[\"'][^\"']*[\"'])?\s*\)"
)
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


# Heading -> relationship scope.  The edge weight belongs to the section in
# which the link appears (not merely to the target node type).
_SCOPE_HEADING_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gotchas", ("gotcha", "anti-pitfall", "antipattern", "pitfall",
                 "落とし穴", "失敗", "アンチパターン")),
    ("synergistic", ("synerg", "combination", "相乗", "組み合わせ")),
    ("workflow", ("workflow", "implementation", "procedure", "手順", "実装")),
    ("taxonomy", ("taxonomy", "classification", "definition", "分類", "定義")),
)


@dataclass
class Link:
    """A Markdown link with its resolved, wiki-root-relative target."""

    text: str
    target: str
    line: int
    resolved: str | None = None
    external: bool = False
    heading: str = ""
    section_scope: str | None = None
    context: str = ""


@dataclass
class Section:
    """One Markdown heading section (text excludes the heading line)."""

    heading: str
    level: int
    text: str
    scope: str | None = None


@dataclass
class Concept:
    """One OKF topic (one file)."""

    path: Path
    rel: str
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    links: list[Link] = field(default_factory=list)

    @property
    def type(self) -> str:
        value = self.meta.get("type")
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        value = self.meta.get("title")
        if isinstance(value, str) and value.strip():
            return value.strip()
        heading = _HEADING_RE.search(self.body)
        if heading:
            return heading.group(1).strip()
        return self.path.stem

    @property
    def description(self) -> str:
        value = self.meta.get("description")
        return value.strip() if isinstance(value, str) else ""

    @property
    def tags(self) -> list[str]:
        value = self.meta.get("tags")
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    @property
    def timestamp(self) -> str | None:
        value = self.meta.get("timestamp")
        return value if isinstance(value, str) else None

    @property
    def resource(self) -> str | None:
        value = self.meta.get("resource")
        return value if isinstance(value, str) else None

    @property
    def scope(self) -> str:
        return scope_of(self.meta)

    @property
    def basis(self) -> str | None:
        value = self.meta.get("basis")
        return value if isinstance(value, str) and value else None

    @property
    def sources(self) -> list[str]:
        value = self.meta.get("sources")
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    @property
    def verified_at(self) -> str | None:
        value = self.meta.get("verified_at")
        return value if isinstance(value, str) and value else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.rel,
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "scope": self.scope,
            "timestamp": self.timestamp,
            "resource": self.resource,
            "basis": self.basis,
            "sources": self.sources,
            "verified_at": self.verified_at,
        }


def scope_from_heading(heading: str) -> str | None:
    """Map a section heading such as ``## Gotchas / Anti-Pitfalls`` to a scope."""
    value = heading.casefold()
    if not value:
        return None
    for scope, needles in _SCOPE_HEADING_PATTERNS:
        if any(needle in value for needle in needles):
            return scope
    return None


def tokenize(text: str) -> list[str]:
    """Deterministic, dependency-free tokenizer for relevance scoring."""
    return _TOKEN_RE.findall(text.casefold())


def relevance(query_tokens: set[str] | list[str] | tuple[str, ...],
              text: str) -> float:
    """``|Q ∩ Tokens(text)| / |Q|``; 1.0 for an empty query."""
    wanted = set(query_tokens)
    if not wanted:
        return 1.0
    return len(wanted & set(tokenize(text))) / len(wanted)


def scope_of(meta: dict[str, Any]) -> str:
    """Classify a node into one of the traversal scope weights."""
    explicit = meta.get("scope")
    if isinstance(explicit, str) and explicit.casefold() in SCOPE_WEIGHTS:
        return explicit.casefold()

    type_name = str(meta.get("type") or "").casefold()
    tags = {str(tag).casefold() for tag in (meta.get("tags") or [])
            if isinstance(meta.get("tags"), list)}
    haystack = type_name + " " + " ".join(tags)

    if "gotcha" in haystack:
        return "gotchas"
    if "synerg" in haystack:
        return "synergistic"
    if "workflow" in haystack or "procedure" in haystack:
        return "workflow"
    return "taxonomy"


def validate_meta(meta: dict[str, Any]) -> list[str]:
    """Return human-readable OKF metadata errors (empty when valid)."""
    errors: list[str] = []
    if not isinstance(meta, dict):
        return ["frontmatter must be a mapping"]

    type_value = meta.get("type")
    if not isinstance(type_value, str) or not type_value.strip():
        errors.append("missing required field: type")

    for key in ("title", "description", "resource", "timestamp"):
        value = meta.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{key} must be a string")

    tags = meta.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(
                isinstance(tag, str) for tag in tags):
            errors.append("tags must be a list of strings")

    timestamp = meta.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"invalid timestamp: {timestamp}")

    scope = meta.get("scope")
    if scope is not None:
        if not isinstance(scope, str) or scope.casefold() not in SCOPE_WEIGHTS:
            errors.append(f"unknown scope: {scope}")

    basis = meta.get("basis")
    if basis is not None:
        if not isinstance(basis, str) or basis not in BASIS_VALUES:
            errors.append(f"invalid basis: {basis}")

    sources = meta.get("sources")
    if sources is not None:
        if not isinstance(sources, list):
            errors.append("sources must be a list of strings")
        else:
            for item in sources:
                if not isinstance(item, str):
                    errors.append("sources items must be strings")
                    continue
                try:
                    parse_source_entry(item)
                except ValueError as exc:
                    errors.append(f"invalid source entry: {exc}")

    verified_at = meta.get("verified_at")
    if verified_at is not None:
        if (not isinstance(verified_at, str)
                or not _is_valid_iso_date(verified_at)):
            errors.append(f"invalid verified_at: {verified_at}")
    return errors


def extract_links(body: str, source_rel: str) -> list[Link]:
    """Extract non-image Markdown links, ignoring fenced blocks and code.

    Each link remembers its nearest heading and the nearest recognised
    standard section scope (e.g. ``Gotchas / Anti-Pitfalls``).  Deeper
    subheadings inherit an outer recognised scope.
    """
    links: list[Link] = []
    fence: str | None = None
    stack: list[tuple[int, str, str | None]] = []
    for number, line in enumerate(body.splitlines(), start=1):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            continue

        heading = _ANY_HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text, scope_from_heading(text)))

        current_heading = next(
            (text for _, text, _ in reversed(stack)
             if not is_transparent_heading(text)),
            "",
        )
        section_scope = next(
            (scope for _, _, scope in reversed(stack) if scope), None)

        for match in _LINK_RE.finditer(_INLINE_CODE_RE.sub("", line)):
            target = match.group(2).strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            if not target or target.startswith("#"):
                continue
            external = bool(_SCHEME_RE.match(target)) or target.startswith("//")
            if external:
                continue  # relative links only: they are the graph edges
            anchor = match.group(1)
            context = " ".join(part for part in
                               (anchor, current_heading, line.strip()) if part)
            links.append(Link(
                text=anchor, target=target, line=number,
                resolved=resolve_link(source_rel, target), external=False,
                heading=current_heading, section_scope=section_scope,
                context=context,
            ))
    return links


def find_wikilinks(body: str) -> list[tuple[int, str]]:
    """Find unsupported ``[[WikiLink]]`` / ``![[embed]]`` syntax.

    The canonical OKF link form is a relative Markdown link.  This helper lets
    validation warn instead of silently ignoring links written in Obsidian's
    vault-wide syntax (also inside fenced code blocks, which are skipped).
    """
    found: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(body.splitlines(), start=1):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            continue
        for match in _WIKILINK_RE.finditer(_INLINE_CODE_RE.sub("", line)):
            found.append((number, match.group(0)))
    return found


def extract_sections(body: str) -> list[Section]:
    """Split Markdown into heading sections, ignoring fenced code blocks."""
    sections: list[Section] = []
    stack: list[tuple[int, str, str | None]] = []
    heading, level, scope = "", 0, None
    buffer: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if heading or buffer:
            sections.append(Section(heading=heading, level=level,
                                    text="\n".join(buffer).strip("\n"),
                                    scope=scope))

    for line in body.splitlines():
        stripped = line.lstrip()
        if fence:
            buffer.append(line)
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            buffer.append(line)
            continue
        match = _ANY_HEADING_RE.match(stripped)
        if match:
            flush()
            buffer = []
            level = len(match.group(1))
            heading = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading, scope_from_heading(heading)))
            scope = next((item[2] for item in reversed(stack) if item[2]), None)
        else:
            buffer.append(line)
    flush()
    return sections


def resolve_link(source_rel: str, target: str) -> str | None:
    """Resolve ``target`` relative to ``source_rel`` inside the wiki root.

    Returns a POSIX relative path, or ``None`` when the link escapes the root
    (or is absolute). The target file does not have to exist.
    """
    clean = unquote(target.split("#", 1)[0].split("?", 1)[0]).strip()
    if not clean:
        return None
    if clean.startswith("/"):
        candidate = posixpath.normpath(clean.lstrip("/"))
    else:
        candidate = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_rel), clean))
    if candidate in ("", ".", "..") or candidate.startswith("../"):
        return None
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        return None
    return candidate


def load_concept(path: str | Path, root: str | Path) -> Concept:
    """Load and parse one Markdown file as an OKF concept."""
    path = Path(path)
    root = Path(root)
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise FrontmatterError(f"{path} is outside the wiki root") from exc
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    return Concept(path=path, rel=rel, meta=meta, body=body,
                   links=extract_links(body, rel))


def iter_concepts(root: str | Path) -> list[Concept]:
    """Return all concept nodes under ``root``, sorted by relative path."""
    root = Path(root)
    concepts: list[Concept] = []
    if not root.exists():
        return concepts
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if is_special(rel):
            continue
        concepts.append(load_concept(path, root))
    return concepts


def write_concept(path: str | Path, meta: dict[str, Any],
                  body: str = "") -> Path:
    """Atomically write a concept document (``.tmp`` + ``os.replace``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(dump_document(meta, body), encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def is_special(rel: str) -> bool:
    parts = rel.split("/")
    if any(part.startswith(".") or part == "__pycache__" for part in parts):
        return True
    return parts[-1] in SPECIAL_FILES or bool(_INDEX_RE.match(parts[-1]))
