"""Turn EPO publications into OKF concept nodes (cache-first, fair use).

The ingester is intentionally thin: it only reads through an ``OpsClient``
(or a compatible fake), writes atomic Markdown files and regenerates indexes.
No network access happens here, so the free-plan rate limiter in ``epo.py``
remains the single gate for OPS traffic.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..epo import EpoError, PublicationRef, normalize_docdb
from .frontmatter import FrontmatterError, parse_frontmatter
from .index import write_indexes
from .model import load_concept, write_concept

__all__ = ["ingest_publication", "ingest_query"]

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_VOLATILE_META_KEYS = frozenset({"timestamp", "verified_at"})


def ingest_publication(
    client: Any,
    publication: str,
    root: str | Path,
    *,
    include_family: bool = True,
    include_claims: bool = True,
    create_stubs: bool = True,
    eps_client: Any | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Ingest one publication, its claims and (optionally) family stubs."""
    wiki = Path(root)
    wiki.mkdir(parents=True, exist_ok=True)
    timestamp = now or _utcnow()
    docdb = normalize_docdb(publication)
    compact = docdb.replace(".", "")

    biblio = client.biblio(compact) or {}
    warnings: list[str] = []

    claims: list[str] = []
    claims_source: str | None = None
    if include_claims:
        claim_errors: list[str] = []
        try:
            claims = list(client.claims(compact) or [])
        except EpoError as exc:
            claim_errors.append(f"OPS: {exc}")
        if claims:
            claims_source = "EPO OPS"
        elif eps_client is not None and compact[:2].upper() == "EP":
            try:
                claims = list(eps_client.claims(compact) or [])
            except EpoError as exc:
                claim_errors.append(f"EPS: {exc}")
            if claims:
                claims_source = "EPO EPS"
        if not claims and claim_errors:
            warnings.append(
                f"claims unavailable for {compact}: "
                + "; ".join(claim_errors))

    members: list[PublicationRef] = []
    if include_family:
        try:
            members = list(client.family(compact) or [])
        except EpoError as exc:
            warnings.append(f"family unavailable for {compact}: {exc}")

    stubs: list[str] = []
    if include_family and create_stubs:
        for member in members:
            member_compact = _compact_ref(member)
            if not member_compact or member_compact == compact:
                continue
            rel = f"patents/{member_compact}.md"
            if _is_full_node(wiki, rel):
                continue
            _write_concept_if_changed(
                wiki / rel,
                _stub_meta(member_compact, compact, timestamp),
                _stub_body(member_compact, compact),
            )
            stubs.append(rel)

    linkable = {
        _compact_ref(member) for member in members
        if (wiki / f"patents/{_compact_ref(member)}.md").exists()
    }
    node_rel = f"patents/{compact}.md"
    _write_concept_if_changed(
        wiki / node_rel,
        _patent_meta(compact, biblio, timestamp,
                     claims_source=claims_source),
        _patent_body(compact, biblio, claims, members,
                     include_claims=include_claims,
                     include_family=include_family,
                     linkable=linkable),
    )
    _append_log(wiki, timestamp, compact, len(claims), len(members))
    index_files = write_indexes(wiki)

    return {
        "publication": compact,
        "node": node_rel,
        "claims": len(claims),
        "claims_source": claims_source,
        "family": len(members),
        "stubs": stubs,
        "warnings": warnings,
        "index_files": index_files,
    }


def ingest_query(
    client: Any,
    cql: str,
    root: str | Path,
    *,
    max_results: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a CQL search and ingest up to the OPS free-plan window."""
    end = max(1, min(int(max_results), 25))
    result = client.search(cql, begin=1, end=end)
    summaries = [
        ingest_publication(client, _compact_ref(pub), root, **kwargs)
        for pub in result.publications
    ]
    return {
        "query": cql,
        "count": len(summaries),
        "publications": summaries,
    }


# -- document rendering -----------------------------------------------------

def _patent_meta(compact: str, biblio: dict[str, Any],
                 timestamp: str,
                 claims_source: str | None = None) -> dict[str, Any]:
    title = str(biblio.get("title") or "").strip() or compact
    applicants = _names(biblio.get("applicants"))
    country = str(biblio.get("country") or compact[:2]).lower()
    tags = _dedupe(["epo", "patent", country])
    sources = [f"patent-biblio:{compact} via=epo-ops"]
    if claims_source == "EPO OPS":
        sources.append(f"patent-claims:{compact} via=epo-ops")
    elif claims_source == "EPO EPS":
        sources.append(f"patent-claims:{compact} via=epo-eps")
    meta: dict[str, Any] = {
        "type": "Patent",
        "title": title,
        "description": f"EPO patent {compact}" + (f": {title}" if title else ""),
        "resource": _espacenet_url(compact),
        "tags": tags,
        "timestamp": timestamp,
        "scope": "taxonomy",
        "publication": compact,
        "basis": "external",
        "sources": sources,
        "verified_at": timestamp[:10],
    }
    if applicants:
        meta["applicants"] = applicants
    if biblio.get("family_id"):
        meta["family_id"] = str(biblio["family_id"])
    return meta


def _patent_body(compact: str, biblio: dict[str, Any], claims: list[str],
                 members: list[PublicationRef], *, include_claims: bool,
                 include_family: bool,
                 linkable: set[str] | None = None) -> str:
    title = str(biblio.get("title") or "").strip() or compact
    lines = [f"# {title}", "", "## Bibliographic", ""]
    rows = [
        ("Publication", compact),
        ("Applicants", ", ".join(_names(biblio.get("applicants")))),
        ("Inventors", ", ".join(_names(biblio.get("inventors")))),
        ("Priority", str(biblio.get("priority") or "")),
        ("Publication date", _iso_date(biblio.get("publication_date"))),
        ("Family ID", str(biblio.get("family_id") or "")),
    ]
    lines.extend(["| Field | Value |", "| --- | --- |"])
    lines.extend(f"| {key} | {_cell(value)} |" for key, value in rows)

    if include_claims:
        lines.extend(["", "## Claims", ""])
        if claims:
            lines.extend(_claims_block(claims))
        else:
            lines.append(
                "_Claims not available from EPO OPS (free plan or not "
                "published)._")

    others = [member for member in members
              if _compact_ref(member) and _compact_ref(member) != compact]
    if include_family and others:
        lines.extend(["", "## Family", ""])
        for member in others:
            member_compact = _compact_ref(member)
            date = _iso_date(member.date)
            suffix = f" — {date}" if date else ""
            if linkable is None or member_compact in linkable:
                lines.append(
                    f"- [{member_compact}]({member_compact}.md){suffix}")
            else:
                lines.append(f"- {member_compact}{suffix}")
    return "\n".join(lines).rstrip() + "\n"


def _claims_block(claims: list[str]) -> list[str]:
    lines: list[str] = []
    for number, claim in enumerate(claims, start=1):
        text = " ".join(str(claim).split())
        if not text:
            continue
        if re.match(r"^\d+[.)]\s", text):
            lines.append(text)
        else:
            lines.append(f"{number}. {text}")
        lines.append("")
    return lines[:-1] if lines else lines


def _stub_meta(compact: str, parent: str, timestamp: str) -> dict[str, Any]:
    return {
        "type": "Patent",
        "title": compact,
        "description": f"Family member of {parent} (stub)",
        "resource": _espacenet_url(compact),
        "tags": _dedupe(["epo", "patent", compact[:2].lower(), "stub"]),
        "timestamp": timestamp,
        "scope": "taxonomy",
        "publication": compact,
        "basis": "external",
        "sources": [f"patent-family:{parent}"],
    }


def _stub_body(compact: str, parent: str) -> str:
    return (f"# {compact}\n\nFamily member of [{parent}]({parent}.md). "
            "Not yet ingested.\n")


# -- helpers ----------------------------------------------------------------

def _substantive_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in meta.items()
        if key not in _VOLATILE_META_KEYS
    }


def _is_substantively_unchanged(
    target: Path,
    meta: Mapping[str, Any],
    body: str,
) -> bool:
    if not target.exists():
        return False
    try:
        existing_meta, existing_body = parse_frontmatter(
            target.read_text(encoding="utf-8")
        )
    except (OSError, FrontmatterError):
        return False
    return (
        _substantive_meta(existing_meta) == _substantive_meta(meta)
        and existing_body == body
    )


def _write_concept_if_changed(
    target: Path,
    meta: dict[str, Any],
    body: str = "",
) -> bool:
    if _is_substantively_unchanged(target, meta, body):
        return False
    write_concept(target, meta, body)
    return True


def _is_full_node(wiki: Path, rel: str) -> bool:
    target = wiki / rel
    if not target.exists():
        return False
    try:
        concept = load_concept(target, wiki)
    except FrontmatterError:
        return True  # leave malformed user files untouched
    return "stub" not in [tag.casefold() for tag in concept.tags]


def _append_log(wiki: Path, timestamp: str, compact: str, claims: int,
                family: int) -> None:
    log = wiki / "log.md"
    if not log.exists():
        log.write_text("# Ingestion Log\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            f"- {timestamp} — ingested {compact} "
            f"(claims: {claims}, family: {family})\n")


def _compact_ref(ref: PublicationRef) -> str:
    return "".join(part for part in (ref.country, ref.number, ref.kind) if part)


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _cell(value: str) -> str:
    return value.replace("|", "\\|") if value else "—"


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()
    match = _DATE_RE.match(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return text


def _espacenet_url(compact: str) -> str:
    return ("https://worldwide.espacenet.com/patent/search?q=pn%3D"
            + quote(compact, safe=""))


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
