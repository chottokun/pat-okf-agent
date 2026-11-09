"""Structural validation for an OKF wiki."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .frontmatter import FrontmatterError, parse_frontmatter
from .model import (
    BASIS_VALUES,
    KNOWN_SOURCE_KINDS,
    NEWS_SOURCE_KINDS,
    SOURCE_ATTR_KEYS,
    SourceEntry,
    WEB_SOURCE_KINDS,
    _ANY_HEADING_RE,
    _INLINE_CODE_RE,
    _ISO_DATE_STRICT_RE,
    _LINK_RE,
    _SCHEME_RE,
    extract_links,
    find_wikilinks,
    is_special,
    parse_source_entry,
    resolve_link,
    validate_meta,
)
from .verify import classify_target, snapshot_key_for_url

__all__ = [
    "EvidenceReport",
    "STRICT_PROVENANCE_RULES",
    "ValidationReport",
    "validate_evidence",
    "validate_provenance",
    "validate_wiki",
]

STRICT_PROVENANCE_RULES = frozenset({"P2", "P4", "P5", "E1"})

_UNVERIFIED_ALERT_START_RE = re.compile(
    r"^>\s*\[!NOTE\]\s*(?:一般知識（未検証）|General Knowledge \(Unverified\))\s*$",
    re.IGNORECASE,
)
_UNVERIFIED_HEADING_RE = re.compile(
    r"^(#{2,6})\s*(?:一般知識（未検証）|General Knowledge \(Unverified\))\s*$",
    re.IGNORECASE,
)
_UNVERIFIED_COL_RE = re.compile(
    r"(?:（一般知識・未検証）|\(unverified\))",
    re.IGNORECASE,
)

_PATENT_NUM_RE = re.compile(r"\b[A-Z]{2}\d+[A-Z0-9]*\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_VERSION_RE = re.compile(r"\bv\d+(?:\.\d+)*\b")
_UNIT_NUMBER_RE = re.compile(
    r"(?:\d+(?:\.\d+)?(?:\s*[–\-~〜]\s*\d+(?:\.\d+)?)?)\s*"
    r"(?:"
    r"W/(?:m[·*]?K)|W/m[·*]?K|ppm/K|J/(?:g[·*]?K)|J/g[·*]?K|g/cm[3³]|"
    r"MPa[·*]?m(?:1/2|½)|ppm|ppb|wt%|vol%|mol%|[%℃°C]|"
    r"GPa|MPa|kPa|mPa[·*]?s|Pa[·*]?s|Pa|kV|mV|V(?![A-Za-z])|"
    r"mA|kA|A(?![A-Za-z])|GHz|MHz|kHz|Hz|nm|µm|um|mm|cm|m(?![A-Za-z])|"
    r"kJ|J(?![A-Za-z])|mg|kg|g(?![A-Za-z])|[kK]?Ω[·*]?cm|[kK]?Ω|K(?![A-Za-z])"
    r")"
)


@dataclass
class EvidenceReport:
    evidence_dir: str
    referenced_count: int = 0
    missing_count: int = 0
    orphan_count: int = 0
    unexplained_orphan_count: int = 0
    tombstone_count: int = 0
    missing: list[dict[str, Any]] = field(default_factory=list)
    orphans: list[dict[str, Any]] = field(default_factory=list)
    tombstones: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_dir": self.evidence_dir,
            "referenced_count": self.referenced_count,
            "missing_count": self.missing_count,
            "orphan_count": self.orphan_count,
            "unexplained_orphan_count": self.unexplained_orphan_count,
            "tombstone_count": self.tombstone_count,
            "missing": list(self.missing),
            "orphans": list(self.orphans),
            "tombstones": list(self.tombstones),
        }


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    concept_count: int = 0
    evidence: EvidenceReport | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        d: dict[str, Any] = {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "concept_count": self.concept_count,
        }
        if self.evidence is not None:
            d["evidence"] = self.evidence.as_dict()
        return d


def _is_stub(meta: dict[str, Any]) -> bool:
    tags = meta.get("tags")
    if isinstance(tags, list):
        return any(str(tag).strip().casefold() == "stub" for tag in tags)
    return False


def _is_valid_iso_date(value: str) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_STRICT_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _calc_body_offset(text: str) -> int:
    """Return line count of frontmatter header before body."""
    text = text.lstrip("\ufeff")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return 0
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return idx + 1
    return 0


def _has_unit_number(text: str) -> bool:
    cleaned = _INLINE_CODE_RE.sub(" ", text)
    cleaned = _LINK_RE.sub(r" \1 ", cleaned)
    cleaned = _PATENT_NUM_RE.sub(" ", cleaned)
    cleaned = _DATE_RE.sub(" ", cleaned)
    cleaned = _VERSION_RE.sub(" ", cleaned)
    return bool(_UNIT_NUMBER_RE.search(cleaned))


def _has_source_backing(
    rel: str,
    text: str,
    external_nodes: set[str],
    source_ids: set[str],
) -> bool:
    for match in _LINK_RE.finditer(_INLINE_CODE_RE.sub("", text)):
        target = match.group(2).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        if not target or target.startswith("#"):
            continue
        if bool(_SCHEME_RE.match(target)) or target.startswith("//"):
            return True
        resolved = resolve_link(rel, target)
        if resolved and resolved in external_nodes:
            return True
    for sid in source_ids:
        if sid and sid in text:
            return True
    return False


def _check_p4_blocks(
    rel: str,
    body: str,
    external_nodes: set[str],
    source_ids: set[str],
    line_offset: int = 0,
) -> list[tuple[str, str]]:
    lines = body.splitlines()
    fenced_lines: set[int] = set()
    unverified_lines: set[int] = set()

    fence: str | None = None
    for idx, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if fence:
            fenced_lines.add(idx)
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            fenced_lines.add(idx)

    in_alert = False
    unverified_heading_level: int | None = None

    for idx, line in enumerate(lines, start=1):
        if idx in fenced_lines:
            continue
        stripped = line.strip()

        if in_alert:
            if stripped.startswith(">"):
                unverified_lines.add(idx)
                continue
            in_alert = False

        heading_m = _ANY_HEADING_RE.match(line)
        if heading_m:
            lvl = len(heading_m.group(1))
            if unverified_heading_level is not None and lvl <= unverified_heading_level:
                unverified_heading_level = None

        if _UNVERIFIED_ALERT_START_RE.match(stripped):
            in_alert = True
            unverified_lines.add(idx)
            continue

        if heading_m and _UNVERIFIED_HEADING_RE.match(stripped):
            unverified_heading_level = len(heading_m.group(1))
            unverified_lines.add(idx)
            continue

        if unverified_heading_level is not None:
            unverified_lines.add(idx)

    # Group non-fenced lines into blocks
    warnings: list[tuple[str, str]] = []
    current_block: list[tuple[int, str]] = []
    in_table = False

    def process_block(blk: list[tuple[int, str]], is_tbl: bool) -> None:
        if not blk:
            return
        if is_tbl and len(blk) >= 2:
            header_line = blk[0][1]
            divider_line = blk[1][1]
            if re.match(r"^\|?[\s:|-]+\|[\s:|-]*$", divider_line.strip()):
                raw_headers = [
                    c.strip()
                    for c in re.split(r"(?<!\\)\|", header_line.strip().lstrip("|").rstrip("|"))
                ]
                unverified_cols = {
                    i
                    for i, h in enumerate(raw_headers)
                    if _UNVERIFIED_COL_RE.search(h)
                }
                for line_no, row_text in blk[2:]:
                    if line_no in unverified_lines:
                        continue
                    cells = [
                        c.strip()
                        for c in re.split(r"(?<!\\)\|", row_text.strip().lstrip("|").rstrip("|"))
                    ]
                    has_num = False
                    for c_idx, cell in enumerate(cells):
                        if c_idx not in unverified_cols and _has_unit_number(cell):
                            has_num = True
                            break
                    if has_num:
                        if not _has_source_backing(rel, row_text, external_nodes, source_ids):
                            warnings.append(
                                ("P4", f"{rel}:{line_no + line_offset}: [P4] unverified numeric value in table row without source backing")
                            )
                return

        # Non-table block
        first_num_line: int | None = None
        for line_no, text in blk:
            if line_no in unverified_lines:
                continue
            if _has_unit_number(text):
                first_num_line = line_no
                break
        if first_num_line is not None:
            block_text = "\n".join(t for _, t in blk)
            if not _has_source_backing(rel, block_text, external_nodes, source_ids):
                warnings.append(
                    ("P4", f"{rel}:{first_num_line + line_offset}: [P4] numeric claim with units outside unverified marker lacks block-local source")
                )

    for idx, line in enumerate(lines, start=1):
        if idx in fenced_lines:
            if current_block:
                process_block(current_block, in_table)
                current_block = []
                in_table = False
            continue

        stripped = line.strip()
        is_heading = bool(_ANY_HEADING_RE.match(line))
        is_empty = not stripped
        line_is_table = bool(stripped.startswith("|") and ("|" in stripped[1:]))

        if is_empty or is_heading:
            if current_block:
                process_block(current_block, in_table)
                current_block = []
                in_table = False
            continue

        if line_is_table != in_table:
            if current_block:
                process_block(current_block, in_table)
                current_block = []
            in_table = line_is_table

        current_block.append((idx, line))

    if current_block:
        process_block(current_block, in_table)

    return warnings

    if current_block:
        process_block(current_block, in_table)

    return warnings


def validate_provenance(
    parsed: dict[str, tuple[dict[str, Any], str, list[Any]]],
    *,
    today: date | None = None,
) -> list[tuple[str, str]]:
    """Validate OKF provenance rules (P1..P8), returning (rule_id, message)."""
    ref_today = today or date.today()
    warnings: list[tuple[str, str]] = []

    external_nodes = {
        rel
        for rel, entry in parsed.items()
        if entry[0].get("basis") == "external" and not _is_stub(entry[0])
    }

    for rel, entry in sorted(parsed.items()):
        meta = entry[0]
        body = entry[1]
        links = entry[2]
        body_offset = entry[3] if len(entry) > 3 else 0
        stub = _is_stub(meta)
        basis = meta.get("basis")
        raw_sources = meta.get("sources")
        valid_sources: list[SourceEntry] = []
        if isinstance(raw_sources, list):
            for s in raw_sources:
                if isinstance(s, str):
                    try:
                        valid_sources.append(parse_source_entry(s))
                    except ValueError:
                        pass
        source_ids = {entry.id for entry in valid_sources if entry.id}
        verified_at = meta.get("verified_at")
        verified_date: date | None = None
        if isinstance(verified_at, str) and _is_valid_iso_date(verified_at):
            try:
                verified_date = date.fromisoformat(verified_at)
            except ValueError:
                pass

        # P1
        if not stub and basis is None:
            warnings.append(("P1", f"{rel}: [P1] missing basis"))

        # P2
        if not stub and basis in ("external", "mixed") and not valid_sources:
            has_ext_link = any(
                link.resolved != rel and link.resolved in external_nodes
                for link in links
            )
            if not has_ext_link:
                warnings.append(
                    ("P2", f"{rel}: [P2] basis={basis} requires sources or a link to an external concept")
                )

        # P3
        if not stub and basis == "external" and verified_at is None:
            warnings.append(("P3", f"{rel}: [P3] basis=external without verified_at"))

        # P4
        if not stub and basis in ("external", "mixed", None):
            if not (basis == "external" and valid_sources):
                warnings.extend(
                    _check_p4_blocks(
                        rel, body, external_nodes, source_ids, line_offset=body_offset
                    )
                )

        # P5
        if basis == "external":
            has_unverified = False
            fence = None
            for line in body.splitlines():
                stripped = line.strip()
                if fence:
                    if stripped.startswith(fence):
                        fence = None
                    continue
                if stripped.startswith("```") or stripped.startswith("~~~"):
                    fence = stripped[:3]
                    continue
                if _UNVERIFIED_ALERT_START_RE.match(stripped) or _UNVERIFIED_HEADING_RE.match(stripped):
                    has_unverified = True
                    break
            if has_unverified:
                warnings.append(
                    ("P5", f"{rel}: [P5] basis=external cannot contain unverified knowledge markers")
                )

        # P6
        for entry in valid_sources:
            if entry.kind in WEB_SOURCE_KINDS and "retrieved" not in entry.attrs:
                warnings.append(
                    ("P6", f"{rel}: [P6] web source {entry.kind}:{entry.id} requires retrieved=YYYY-MM-DD")
                )

        # P7
        for entry in valid_sources:
            if entry.kind not in KNOWN_SOURCE_KINDS:
                warnings.append(
                    ("P7", f"{rel}: [P7] unknown source kind: {entry.kind}")
                )

        # P8
        if basis in ("external", "mixed") and valid_sources:
            if all(entry.kind in NEWS_SOURCE_KINDS for entry in valid_sources):
                dates: list[date] = []
                for entry in valid_sources:
                    d_val = entry.attrs.get("issued") or entry.attrs.get("retrieved")
                    if d_val and _is_valid_iso_date(d_val):
                        try:
                            dates.append(date.fromisoformat(d_val))
                        except ValueError:
                            pass
                if dates:
                    latest_source_date = max(dates)
                    if (ref_today - latest_source_date).days >= 1095:
                        if verified_date is None or (ref_today - verified_date).days >= 1095:
                            warnings.append(
                                ("P8", f"{rel}: [P8] volatile sources ({latest_source_date.isoformat()}) are >= 3 years old as of {ref_today.isoformat()}")
                            )

    return warnings


def validate_evidence(
    parsed: dict[str, tuple[dict[str, Any], str, list[Any], int]],
    evidence_dir: Path | str,
) -> tuple[EvidenceReport, list[tuple[str, str]]]:
    """Validate snapshot evidence files corresponding to URL sources in parsed notes."""
    evidence_path = Path(evidence_dir)
    if not evidence_path.exists() or not evidence_path.is_dir():
        raise FileNotFoundError(f"evidence directory does not exist: {evidence_path}")

    snapshots_dir = evidence_path / "snapshots"
    meta_files: dict[str, Path] = {}
    body_files: dict[str, list[Path]] = {}
    extracted_files: dict[str, Path] = {}

    if snapshots_dir.is_dir():
        for p in snapshots_dir.iterdir():
            if not p.is_file() or p.name.endswith(".tmp"):
                continue
            if p.name.endswith(".meta.json"):
                k = p.name[:-len(".meta.json")]
                meta_files[k] = p
            elif p.name.endswith(".extracted.txt"):
                k = p.name[:-len(".extracted.txt")]
                extracted_files[k] = p
            else:
                k = p.name.rsplit(".", 1)[0]
                body_files.setdefault(k, []).append(p)

    warnings: list[tuple[str, str]] = []
    missing: list[dict[str, Any]] = []

    # Map snapshot key -> list of (rel, entry)
    referenced_by: dict[str, list[tuple[str, SourceEntry]]] = {}

    for rel, entry_tuple in sorted(parsed.items()):
        meta = entry_tuple[0]
        raw_sources = meta.get("sources")
        if not isinstance(raw_sources, list):
            continue
        for s in raw_sources:
            if not isinstance(s, str):
                continue
            try:
                src_entry = parse_source_entry(s)
                classified = classify_target(src_entry.id, kind=src_entry.kind)
            except ValueError:
                continue
            if classified.identifier_type != "url":
                continue
            key = snapshot_key_for_url(classified)
            referenced_by.setdefault(key, []).append((rel, src_entry))

            # E1 Checks
            meta_path = meta_files.get(key)
            if meta_path is None:
                warnings.append((
                    "E1",
                    f"{rel}: [E1] missing snapshot metadata for {src_entry.kind}:{src_entry.id} (expected snapshots/{key}.meta.json)",
                ))
                missing.append({
                    "rel": rel,
                    "source": f"{src_entry.kind}:{src_entry.id}",
                    "key": key,
                    "reason": "missing_metadata",
                    "expected_meta_path": str(snapshots_dir / f"{key}.meta.json"),
                })
            else:
                try:
                    meta_json = json.loads(meta_path.read_text("utf-8"))
                except Exception:
                    meta_json = {}
                deleted_at = meta_json.get("deleted_at")
                if deleted_at and str(deleted_at).strip():
                    warnings.append((
                        "E1",
                        f"{rel}: [E1] source {src_entry.kind}:{src_entry.id} references tombstoned snapshot (snapshots/{key}.meta.json, deleted_at={deleted_at})",
                    ))
                    missing.append({
                        "rel": rel,
                        "source": f"{src_entry.kind}:{src_entry.id}",
                        "key": key,
                        "reason": "tombstone_referenced",
                        "deleted_at": str(deleted_at).strip(),
                        "meta_path": str(meta_path),
                    })
                elif key not in body_files or not body_files[key]:
                    warnings.append((
                        "E1",
                        f"{rel}: [E1] missing snapshot body file for {src_entry.kind}:{src_entry.id} (expected snapshots/{key}.<ext>)",
                    ))
                    missing.append({
                        "rel": rel,
                        "source": f"{src_entry.kind}:{src_entry.id}",
                        "key": key,
                        "reason": "missing_body",
                        "expected_body_pattern": str(snapshots_dir / f"{key}.<ext>"),
                    })

    # Orphans and tombstones (non-referenced snapshots)
    orphans: list[dict[str, Any]] = []
    tombstones: list[dict[str, Any]] = []

    for key, meta_path in sorted(meta_files.items()):
        if key in referenced_by:
            continue
        try:
            meta_json = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            meta_json = {}

        url = meta_json.get("url", "")
        deleted_at = meta_json.get("deleted_at")
        orphan_reason = meta_json.get("orphan_reason")
        retrieved_at = meta_json.get("retrieved_at")
        sha256 = meta_json.get("sha256")
        has_body = key in body_files and bool(body_files[key])

        if has_body:
            clean_reason = orphan_reason.strip() if isinstance(orphan_reason, str) and orphan_reason.strip() else None
            orphans.append({
                "key": key,
                "url": url,
                "retrieved_at": retrieved_at,
                "sha256": sha256,
                "orphan_reason": clean_reason,
                "meta_path": str(meta_path),
            })
            if clean_reason is None:
                warnings.append((
                    "E2",
                    f"snapshots/{key}.meta.json: [E2] orphan snapshot without orphan_reason ({url})",
                ))
        else:
            if deleted_at and str(deleted_at).strip():
                clean_reason = orphan_reason.strip() if isinstance(orphan_reason, str) and orphan_reason.strip() else None
                tombstones.append({
                    "key": key,
                    "url": url,
                    "deleted_at": str(deleted_at).strip(),
                    "orphan_reason": clean_reason,
                    "meta_path": str(meta_path),
                })
            else:
                warnings.append((
                    "E3",
                    f"snapshots/{key}.meta.json: [E3] orphan snapshot metadata without body file",
                ))

    # Stray files in snapshots_dir (E3)
    for key, b_list in sorted(body_files.items()):
        if key not in meta_files:
            for b_path in sorted(b_list):
                warnings.append((
                    "E3",
                    f"snapshots/{b_path.name}: [E3] snapshot file without metadata (.meta.json missing)",
                ))
    for key, ext_path in sorted(extracted_files.items()):
        if key not in meta_files:
            warnings.append((
                "E3",
                f"snapshots/{ext_path.name}: [E3] snapshot file without metadata (.meta.json missing)",
            ))

    unexplained_orphan_count = sum(1 for o in orphans if not o.get("orphan_reason"))

    report = EvidenceReport(
        evidence_dir=str(evidence_path),
        referenced_count=len(referenced_by),
        missing_count=len(missing),
        orphan_count=len(orphans),
        unexplained_orphan_count=unexplained_orphan_count,
        tombstone_count=len(tombstones),
        missing=missing,
        orphans=orphans,
        tombstones=tombstones,
    )
    return report, warnings


def validate_wiki(
    root: str | Path,
    *,
    check_orphans: bool = True,
    today: date | None = None,
    strict_provenance: bool = False,
    check_evidence: bool = False,
    evidence_dir: str | Path | None = None,
) -> ValidationReport:
    root = Path(root)
    report = ValidationReport()
    if not (root / "index.md").exists():
        report.warnings.append(
            "missing index.md (run `okf index` to generate progressive indexes)"
        )

    parsed: dict[str, tuple[dict[str, Any], str, list[Any]]] = {}
    if not root.exists():
        report.errors.append(f"wiki root does not exist: {root}")
        return report

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if is_special(rel):
            continue
        raw_text = path.read_text(encoding="utf-8")
        try:
            meta, body = parse_frontmatter(raw_text)
        except FrontmatterError as exc:
            report.errors.append(f"{rel}: {exc}")
            continue
        body_offset = _calc_body_offset(raw_text)
        for message in validate_meta(meta):
            report.errors.append(f"{rel}: {message}")
        for line, wikilink in find_wikilinks(body):
            report.warnings.append(
                f"{rel}:{line}: unsupported WikiLink syntax {wikilink}; "
                "use [text](relative/path.md)"
            )
        parsed[rel] = (meta, body, extract_links(body, rel), body_offset)
        report.concept_count += 1

    inbound = {rel: 0 for rel in parsed}
    outbound = {rel: 0 for rel in parsed}
    for rel, entry in parsed.items():
        links = entry[2]
        for link in links:
            if link.external:
                continue
            if link.resolved is None:
                report.errors.append(
                    f"{rel}: link escapes the wiki root: {link.target}"
                )
                continue
            if link.resolved == rel:
                continue
            if link.resolved not in parsed:
                report.errors.append(f"{rel}: broken link: {link.target}")
                continue
            outbound[rel] += 1
            inbound[link.resolved] += 1

    if check_orphans:
        for rel in sorted(parsed):
            if inbound[rel] == 0 and outbound[rel] == 0:
                report.warnings.append(f"orphan concept (no links): {rel}")

    # Pass 2: provenance checks
    prov_results = validate_provenance(parsed, today=today)
    for rule_id, message in prov_results:
        if strict_provenance and rule_id in STRICT_PROVENANCE_RULES:
            report.errors.append(message)
        else:
            report.warnings.append(message)

    # Pass 3: evidence checks
    if check_evidence:
        ev_dir = Path(evidence_dir or "data/external/source_verify")
        ev_report, ev_warnings = validate_evidence(parsed, ev_dir)
        report.evidence = ev_report
        for rule_id, message in ev_warnings:
            if strict_provenance and rule_id in STRICT_PROVENANCE_RULES:
                report.errors.append(message)
            else:
                report.warnings.append(message)

    return report
