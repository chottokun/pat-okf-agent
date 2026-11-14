"""Minimal, dependency-free YAML frontmatter subset used by OKF documents.

OKF frontmatter is deliberately flat: scalars, inline lists and block lists.
Keeping the parser small means the wiki stays readable with no third-party
runtime dependency (Pattern A: ``type`` is the only required field).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "FrontmatterError",
    "dump_document",
    "dump_frontmatter",
    "parse_frontmatter",
]

_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?:[ \t]*(.*))?$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_EMPTY = object()

_KEY_ORDER = (
    "type", "title", "description", "resource", "tags", "timestamp",
    "scope", "publication", "basis", "sources", "verified_at",
)


class FrontmatterError(ValueError):
    """Raised when a document has malformed or unsupported frontmatter."""


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(metadata, body)``.

    The document must start with a ``---`` line and close it with ``---`` or
    ``...``. Values are flat YAML scalars/lists; nested mappings are rejected
    with :class:`FrontmatterError` rather than silently mis-parsed.
    """
    if not isinstance(text, str):
        raise FrontmatterError("document must be text")
    text = text.lstrip("\ufeff")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("missing YAML frontmatter (expected '---')")

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        raise FrontmatterError("unclosed YAML frontmatter")

    meta: dict[str, Any] = {}
    pending_key: str | None = None

    for raw in lines[1:end]:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if line[:1] in (" ", "\t"):
            if pending_key is None:
                raise FrontmatterError(
                    f"indented value without a key: {stripped!r}")
            if not stripped.startswith("- "):
                raise FrontmatterError(
                    "nested mappings are not supported in OKF frontmatter")
            if meta[pending_key] is _EMPTY:
                meta[pending_key] = []
            meta[pending_key].append(_parse_scalar(stripped[2:].strip()))
            continue

        if pending_key is not None:
            if meta[pending_key] is _EMPTY:
                meta[pending_key] = None
            pending_key = None

        match = _KEY_RE.match(line)
        if not match:
            raise FrontmatterError(f"invalid frontmatter line: {line!r}")
        key = match.group(1)
        if key in meta:
            raise FrontmatterError(f"duplicate frontmatter key: {key}")
        value = (match.group(2) or "").strip()
        if value == "":
            meta[key] = _EMPTY
            pending_key = key
        else:
            meta[key] = _parse_value(value)

    if pending_key is not None and meta[pending_key] is _EMPTY:
        meta[pending_key] = None
    # ``_EMPTY`` cannot survive the loop above, but keep the invariant explicit.
    for key, value in list(meta.items()):
        if value is _EMPTY:
            meta[key] = None

    body = "".join(lines[end + 1:])
    return dict(meta), body


def dump_document(meta: Mapping[str, Any], body: str = "") -> str:
    """Render ``meta`` and ``body`` as an OKF Markdown document."""
    return dump_frontmatter(meta) + body


def dump_frontmatter(meta: Mapping[str, Any]) -> str:
    """Render deterministic YAML frontmatter (standard keys first)."""
    if not isinstance(meta, Mapping):
        raise FrontmatterError("metadata must be a mapping")
    lines = ["---"]
    keys = [key for key in _KEY_ORDER if key in meta]
    keys += sorted(key for key in meta if key not in _KEY_ORDER)
    for key in keys:
        if not _KEY_RE.match(f"{key}:"):
            raise FrontmatterError(f"invalid frontmatter key: {key!r}")
        lines.append(f"{key}: {_format_value(meta[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# -- parsing helpers --------------------------------------------------------

def _parse_value(value: str) -> Any:
    value = _strip_comment(value).strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in _split_inline(inner)]
    return _parse_scalar(value)


def _strip_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _split_inline(inner: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in inner:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
            continue
        if char == ",":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise FrontmatterError(
                f"invalid double-quoted scalar: {value!r}") from exc
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "~"):
        return None
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        return float(value)
    return value


# -- dumping helpers --------------------------------------------------------

def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _format_scalar(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    raise FrontmatterError(
        f"unsupported frontmatter value type: {type(value).__name__}")


def _format_scalar(value: str) -> str:
    if value == "":
        return '""'
    unsafe = (
        "\n" in value or "\r" in value or "\t" in value
        or value != value.strip()
        or "#" in value or ": " in value or value.endswith(":")
        or value.casefold() in {"yes", "no", "on", "off", "y", "n"}
        or bool(re.match(r"^\d{4}-\d{2}-\d{2}([T ].*)?$", value))
        or value.startswith(("[", "]", "{", "}", "- ", "? ", "! ", "& ",
                            "* ", "|", ">", "'", '"', "%", "@", "`"))
        or value[0] in "!&*|>@`%"
    )
    if unsafe:
        return json.dumps(value, ensure_ascii=False)
    # Quote anything that would round-trip as a bool/number/null.
    if _parse_scalar(value) != value:
        return json.dumps(value, ensure_ascii=False)
    return value
