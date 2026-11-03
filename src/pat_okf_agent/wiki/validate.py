"""Structural validation for an OKF wiki."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import FrontmatterError, parse_frontmatter
from .model import (
    extract_links,
    find_wikilinks,
    is_special,
    validate_meta,
)

__all__ = ["ValidationReport", "validate_wiki"]


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    concept_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": list(self.errors),
                "warnings": list(self.warnings),
                "concept_count": self.concept_count}


def validate_wiki(root: str | Path, *, check_orphans: bool = True
                  ) -> ValidationReport:
    root = Path(root)
    report = ValidationReport()
    if not (root / "index.md").exists():
        report.warnings.append(
            "missing index.md (run `okf index` to generate progressive indexes)")

    parsed: dict[str, tuple[dict, list]] = {}
    if not root.exists():
        report.errors.append(f"wiki root does not exist: {root}")
        return report

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if is_special(rel):
            continue
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except FrontmatterError as exc:
            report.errors.append(f"{rel}: {exc}")
            continue
        for message in validate_meta(meta):
            report.errors.append(f"{rel}: {message}")
        for line, wikilink in find_wikilinks(body):
            report.warnings.append(
                f"{rel}:{line}: unsupported WikiLink syntax {wikilink}; "
                "use [text](relative/path.md)")
        parsed[rel] = (meta, extract_links(body, rel))
        report.concept_count += 1

    inbound = {rel: 0 for rel in parsed}
    outbound = {rel: 0 for rel in parsed}
    for rel, (_meta, links) in parsed.items():
        for link in links:
            if link.external:
                continue
            if link.resolved is None:
                report.errors.append(
                    f"{rel}: link escapes the wiki root: {link.target}")
                continue
            if link.resolved == rel:
                continue
            if link.resolved not in parsed:
                report.errors.append(
                    f"{rel}: broken link: {link.target}")
                continue
            outbound[rel] += 1
            inbound[link.resolved] += 1

    if check_orphans:
        for rel in sorted(parsed):
            if inbound[rel] == 0 and outbound[rel] == 0:
                report.warnings.append(
                    f"orphan concept (no links): {rel}")
    return report
