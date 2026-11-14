"""Deterministic, offline OKF knowledge base JSON exporter (``okf export``)."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .frontmatter import FrontmatterError, parse_frontmatter
from .model import (
    SourceEntry,
    extract_links,
    is_special,
    parse_source_entry,
    resolve_source_genre,
)
from .verify import DEFAULT_CACHE_DIR, snapshot_key_for_url

__all__ = [
    "EXPORT_FORMAT_VERSIONS",
    "EXPORT_SCOPES",
    "export_bundle",
    "export_kb",
    "write_export_bundle",
]

EXPORT_FORMAT_VERSIONS: tuple[int, ...] = (1, 2)
EXPORT_SCOPES: tuple[str, ...] = ("all", "concepts", "patents")

_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_JST = timezone(timedelta(hours=9))


def _safe_cache_stem(raw_id: str) -> str:
    return _SAFE_NAME_RE.sub("_", raw_id.strip())


def _ordered_markdown_files(folder_dir: Path, *, exclude_index: bool = True) -> list[Path]:
    """Return markdown files ordered by first link appearance in ``index.md``, then filename."""
    if not folder_dir.is_dir():
        return []
    candidates = {
        p.name: p
        for p in sorted(folder_dir.glob("*.md"))
        if not (exclude_index and is_special(p.name))
    }
    ordered: list[Path] = []
    seen: set[str] = set()
    index_path = folder_dir / "index.md"
    if index_path.is_file():
        index_text = index_path.read_text(encoding="utf-8", errors="replace")
        for link in extract_links(index_text, f"{folder_dir.name}/index.md"):
            target = (link.resolved or link.target).split("#", 1)[0].split("?", 1)[0]
            name = target.rsplit("/", 1)[-1]
            if name in candidates and name not in seen:
                ordered.append(candidates[name])
                seen.add(name)
    for name, path in candidates.items():
        if name not in seen:
            ordered.append(path)
    return ordered


def _parse_year(issued: str | None) -> int | None:
    if not issued or len(issued) < 4:
        return None
    try:
        return int(issued[:4])
    except ValueError:
        return None


def _strip_arxiv_prefix(raw_id: str) -> str:
    if raw_id.lower().startswith("arxiv:"):
        return raw_id.split(":", 1)[1].strip()
    return raw_id.strip()


def _resolve_arxiv_metadata(
    bare_id: str,
    search_caches: list[Path],
    fallback_issued: str | None,
) -> dict[str, Any] | None:
    versionless = re.sub(r"v\d+$", "", bare_id)
    stems = [_safe_cache_stem(bare_id)]
    if versionless != bare_id:
        stems.append(_safe_cache_stem(versionless))

    for cache in search_caches:
        arxiv_dir = cache / "arxiv"
        if not arxiv_dir.is_dir():
            continue
        for stem in stems:
            xml_path = arxiv_dir / f"{stem}.xml"
            if xml_path.is_file():
                try:
                    root = ET.fromstring(xml_path.read_text(encoding="utf-8", errors="replace"))
                    entry = root.find("atom:entry", _ATOM_NS)
                    if entry is not None:
                        title_el = entry.find("atom:title", _ATOM_NS)
                        title = " ".join(title_el.text.split()) if title_el is not None and title_el.text else None
                        authors = [
                            " ".join(name_el.text.split())
                            for name_el in entry.findall("atom:author/atom:name", _ATOM_NS)
                            if name_el.text and name_el.text.strip()
                        ]
                        pub_el = entry.find("atom:published", _ATOM_NS)
                        issued = pub_el.text.strip()[:10] if pub_el is not None and pub_el.text else fallback_issued
                        jref_el = entry.find("arxiv:journal_ref", _ATOM_NS)
                        venue = " ".join(jref_el.text.split()) if jref_el is not None and jref_el.text else "arXiv"
                        return {
                            "title": title,
                            "authors": authors,
                            "issued": issued,
                            "year": _parse_year(issued),
                            "venue": venue,
                            "url": f"https://arxiv.org/abs/{bare_id}",
                            "metadata_cached": True,
                        }
                except ET.ParseError:
                    pass
            meta_path = arxiv_dir / f"{stem}.meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    issued = meta.get("issued") or fallback_issued
                    return {
                        "title": meta.get("title"),
                        "authors": [],
                        "issued": issued,
                        "year": _parse_year(issued),
                        "venue": "arXiv",
                        "url": f"https://arxiv.org/abs/{bare_id}",
                        "metadata_cached": True,
                    }
                except (OSError, json.JSONDecodeError):
                    pass
    return None


def _extract_crossref_issued(msg: dict[str, Any], fallback_issued: str | None) -> tuple[str | None, int | None]:
    for field in ("published-print", "published-online", "issued", "created"):
        part_obj = msg.get(field)
        if isinstance(part_obj, dict):
            parts = part_obj.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                dp = parts[0]
                year = dp[0] if isinstance(dp[0], int) else None
                if len(dp) >= 3 and all(isinstance(x, int) for x in dp[:3]):
                    return f"{dp[0]:04d}-{dp[1]:02d}-{dp[2]:02d}", year
                if len(dp) >= 2 and all(isinstance(x, int) for x in dp[:2]):
                    return f"{dp[0]:04d}-{dp[1]:02d}", year
                if year is not None:
                    return str(year), year
    return fallback_issued, _parse_year(fallback_issued)


def _resolve_doi_metadata(
    bare_doi: str,
    search_caches: list[Path],
    fallback_issued: str | None,
) -> dict[str, Any] | None:
    stem = _safe_cache_stem(bare_doi)
    for cache in search_caches:
        cr_path = cache / "crossref" / f"{stem}.json"
        if cr_path.is_file():
            try:
                data = json.loads(cr_path.read_text(encoding="utf-8"))
                msg = data.get("message") if isinstance(data, dict) else None
                if isinstance(msg, dict):
                    titles = msg.get("title") or []
                    title = titles[0] if isinstance(titles, list) and titles else (titles if isinstance(titles, str) else None)
                    authors = []
                    for a in msg.get("author") or []:
                        if isinstance(a, dict):
                            full = " ".join(x for x in (a.get("given"), a.get("family")) if x).strip()
                            if full:
                                authors.append(full)
                    containers = msg.get("container-title") or []
                    venue = containers[0] if isinstance(containers, list) and containers else (containers if isinstance(containers, str) else None)
                    issued, year = _extract_crossref_issued(msg, fallback_issued)
                    return {
                        "title": title,
                        "authors": authors,
                        "issued": issued,
                        "year": year,
                        "venue": venue,
                        "url": msg.get("URL") or f"https://doi.org/{bare_doi}",
                        "metadata_cached": True,
                    }
            except (OSError, json.JSONDecodeError):
                pass
        oa_path = cache / "openalex" / f"{stem}.json"
        if oa_path.is_file():
            try:
                oa = json.loads(oa_path.read_text(encoding="utf-8"))
                if isinstance(oa, dict):
                    year = oa.get("publication_year")
                    issued = oa.get("publication_date") or fallback_issued
                    authors = [
                        a.get("author", {}).get("display_name", "").strip()
                        for a in oa.get("authorships") or []
                        if isinstance(a, dict) and a.get("author", {}).get("display_name")
                    ]
                    loc = oa.get("primary_location") or {}
                    src = loc.get("source") or {}
                    venue = src.get("display_name") if isinstance(src, dict) else None
                    return {
                        "title": oa.get("title"),
                        "authors": authors,
                        "issued": issued,
                        "year": year if isinstance(year, int) else _parse_year(issued),
                        "venue": venue,
                        "url": oa.get("doi") or f"https://doi.org/{bare_doi}",
                        "metadata_cached": True,
                    }
            except (OSError, json.JSONDecodeError):
                pass
    return None


def _resolve_snapshot_metadata(
    entry: SourceEntry,
    search_caches: list[Path],
    fallback_issued: str | None,
) -> dict[str, Any] | None:
    snap_key = entry.attrs.get("snapshot")
    if not snap_key and entry.id.startswith(("http://", "https://")):
        snap_key = snapshot_key_for_url(entry.id)
    if not snap_key:
        return None
    for cache in search_caches:
        meta_path = cache / "snapshots" / f"{snap_key}.meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                issued = meta.get("extracted_issued") or fallback_issued
                return {
                    "title": meta.get("title"),
                    "authors": [],
                    "issued": issued,
                    "year": _parse_year(issued),
                    "venue": meta.get("extracted_publisher"),
                    "url": meta.get("final_url") or meta.get("url") or (entry.id if entry.id.startswith(("http://", "https://")) else None),
                    "metadata_cached": True,
                }
            except (OSError, json.JSONDecodeError):
                pass
    return None


def _resolve_paper_record(
    entry: SourceEntry,
    search_caches: list[Path],
) -> tuple[dict[str, Any], str | None]:
    """Resolve bibliographic fields for a paper source purely from local caches."""
    fallback_issued = entry.attrs.get("issued")
    is_arxiv = entry.kind == "preprint" or entry.id.lower().startswith("arxiv:")
    resolved: dict[str, Any] | None = None
    default_url: str | None = None

    if is_arxiv:
        bare_id = _strip_arxiv_prefix(entry.id)
        default_url = f"https://arxiv.org/abs/{bare_id}"
        resolved = _resolve_arxiv_metadata(bare_id, search_caches, fallback_issued)
    elif entry.kind == "paper" or entry.id.startswith("10."):
        bare_doi = entry.id.strip()
        default_url = f"https://doi.org/{bare_doi}"
        resolved = _resolve_doi_metadata(bare_doi, search_caches, fallback_issued)
    else:
        if entry.id.startswith(("http://", "https://")):
            default_url = entry.id
        resolved = _resolve_snapshot_metadata(entry, search_caches, fallback_issued)

    if resolved is not None:
        title = resolved.get("title") or None
        warning = None
        if title is None:
            warning = f"missing bibliographic title in local cache for paper: {entry.id}"
        return {
            "id": entry.id,
            "kind": entry.kind,
            "genre": "paper",
            "title": title,
            "authors": resolved.get("authors", []),
            "year": resolved.get("year"),
            "issued": resolved.get("issued"),
            "venue": resolved.get("venue"),
            "url": resolved.get("url") or default_url,
            "metadata_cached": True,
        }, warning

    warning = f"missing local metadata cache for paper: {entry.id}"
    return {
        "id": entry.id,
        "kind": entry.kind,
        "genre": "paper",
        "title": None,
        "authors": [],
        "year": _parse_year(fallback_issued),
        "issued": fallback_issued,
        "venue": None,
        "url": default_url,
        "metadata_cached": False,
    }, warning


def _parse_note(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return parse_frontmatter(text)
    except FrontmatterError:
        return {}, text


def export_kb(
    kb_dir: str | Path,
    *,
    label: str | None = None,
    cache_dirs: list[Path] | None = None,
    format_version: int = 2,
    scope: str = "all",
) -> dict[str, Any]:
    """Export one OKF KB directory into a deterministic viewer-ready dictionary."""
    if format_version not in EXPORT_FORMAT_VERSIONS:
        raise ValueError(f"Unsupported format_version: {format_version!r}")
    if scope not in EXPORT_SCOPES:
        raise ValueError(f"Unsupported scope: {scope!r}")

    kb_path = Path(kb_dir)
    kb_id = kb_path.name
    effective_label = label or kb_id

    search_caches: list[Path] = []
    ev_dir = kb_path / "evidence"
    if ev_dir.is_dir():
        search_caches.append(ev_dir)
    for c in cache_dirs if cache_dirs is not None else [DEFAULT_CACHE_DIR]:
        c_path = Path(c)
        if c_path.is_dir() and c_path not in search_caches:
            search_caches.append(c_path)

    concept_files = _ordered_markdown_files(kb_path / "concepts", exclude_index=True)
    patent_files = _ordered_markdown_files(kb_path / "patents", exclude_index=True)
    concept_slugs = {p.stem for p in concept_files}

    concepts: list[dict[str, Any]] = []
    patent_to_concepts: dict[str, list[str]] = {}
    papers_by_id: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    seen_warnings: set[str] = set()

    def _add_warning(msg: str) -> None:
        if msg not in seen_warnings:
            seen_warnings.add(msg)
            warnings.append(msg)

    for p in concept_files:
        fm, body = _parse_note(p)
        slug = p.stem
        links: list[str] = []
        patent_links: list[str] = []
        for link in extract_links(body, f"concepts/{p.name}"):
            if link.external:
                continue
            target = (link.resolved or link.target).split("#", 1)[0].split("?", 1)[0]
            if not target.endswith(".md"):
                continue
            base = target.rsplit("/", 1)[-1][:-3]
            if target.startswith("patents/") or "/patents/" in target:
                lst = patent_to_concepts.setdefault(base, [])
                if slug not in lst:
                    lst.append(slug)
                if base not in patent_links:
                    patent_links.append(base)
            elif base in concept_slugs and base != slug and base not in links:
                links.append(base)

        raw_sources = fm.get("sources") or []
        if not isinstance(raw_sources, list):
            raw_sources = []

        structured_sources: list[dict[str, Any]] = []
        paper_ids: list[str] = []
        patent_source_ids: list[str] = list(patent_links)

        if format_version >= 2:
            for raw in raw_sources:
                if not isinstance(raw, str):
                    continue
                try:
                    entry = parse_source_entry(raw)
                except ValueError as exc:
                    _add_warning(f"unparseable source in {slug}: {raw!r} ({exc})")
                    continue
                genre = resolve_source_genre(entry)
                structured_sources.append({
                    "raw": entry.raw,
                    "kind": entry.kind,
                    "genre": genre,
                    "id": entry.id,
                    "issued": entry.attrs.get("issued"),
                    "retrieved": entry.attrs.get("retrieved"),
                    "via": entry.attrs.get("via"),
                    "snapshot": entry.attrs.get("snapshot"),
                })
                if genre == "paper":
                    if entry.id not in paper_ids:
                        paper_ids.append(entry.id)
                    if entry.id not in papers_by_id:
                        rec, warn = _resolve_paper_record(entry, search_caches)
                        rec["related_concepts"] = []
                        papers_by_id[entry.id] = rec
                        if warn:
                            _add_warning(warn)
                    rel = papers_by_id[entry.id]["related_concepts"]
                    if slug not in rel:
                        rel.append(slug)
                elif genre == "patent" and entry.id not in patent_source_ids:
                    patent_source_ids.append(entry.id)

        if scope == "patents":
            continue

        concept_obj: dict[str, Any] = {
            "slug": slug,
            "type": fm.get("type"),
            "title": fm.get("title"),
            "description": fm.get("description"),
            "tags": fm.get("tags") or [],
            "scope": fm.get("scope"),
            "basis": fm.get("basis"),
            "sources_count": len(raw_sources),
            "verified_at": fm.get("verified_at"),
            "links": links,
            "body": body,
        }
        if format_version >= 2:
            concept_obj["sources"] = structured_sources
            concept_obj["paper_ids"] = paper_ids
            concept_obj["patent_ids"] = patent_source_ids
        concepts.append(concept_obj)

    patents: list[dict[str, Any]] = []
    if scope != "concepts":
        for p in patent_files:
            fm, _b = _parse_note(p)
            patents.append({
                "slug": p.stem,
                "title": fm.get("title"),
                "tags": fm.get("tags") or [],
                "publication": fm.get("publication"),
                "related_concepts": patent_to_concepts.get(p.stem, []),
            })

    groups: list[Any] = []
    syn_path = kb_path / "synonyms.json"
    if scope != "patents" and syn_path.is_file():
        try:
            groups = json.loads(syn_path.read_text(encoding="utf-8")).get("groups") or []
        except (OSError, json.JSONDecodeError) as exc:
            _add_warning(f"failed to parse synonyms.json: {exc}")

    papers = list(papers_by_id.values()) if (format_version >= 2 and scope != "patents") else []

    stats: dict[str, Any] = {
        "concepts": len(concepts),
        "patents": len(patents),
        "total_notes": len(concepts) + len(patents),
        "synonym_groups": len(groups),
    }
    if format_version >= 2:
        stats["papers"] = len(papers)

    result: dict[str, Any] = {
        "id": kb_id,
        "label": effective_label,
        "export_scope": scope,
        "partial": scope != "all",
        "stats": stats,
        "concepts": concepts,
        "patents": patents,
        "synonyms": {"groups": groups},
    }
    if format_version >= 2:
        result["papers"] = papers
        result["warnings"] = warnings
    return result


def export_bundle(
    kb_specs: list[tuple[str | Path, str | None]],
    *,
    cache_dirs: list[Path] | None = None,
    format_version: int = 2,
    scope: str = "all",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Export one or more KBs into a single viewer JSON bundle."""
    if not kb_specs:
        raise ValueError("at least one KB directory is required")
    if format_version not in EXPORT_FORMAT_VERSIONS:
        raise ValueError(f"Unsupported format_version: {format_version!r}")
    if scope not in EXPORT_SCOPES:
        raise ValueError(f"Unsupported scope: {scope!r}")

    kbs = [
        export_kb(
            kb_dir,
            label=label,
            cache_dirs=cache_dirs,
            format_version=format_version,
            scope=scope,
        )
        for kb_dir, label in kb_specs
    ]
    first = kbs[0]
    ts = generated_at or datetime.now(_JST).isoformat(timespec="seconds")
    bundle: dict[str, Any] = {
        "format_version": format_version,
        "export_scope": scope,
        "partial": scope != "all",
        "generated_at": ts,
        "kb": first["id"],
        "stats": first["stats"],
        "concepts": first["concepts"],
        "patents": first["patents"],
        "synonyms": first["synonyms"],
        "kbs": kbs,
    }
    if format_version >= 2:
        bundle["papers"] = first.get("papers", [])
        all_warnings: list[str] = []
        for kb in kbs:
            for w in kb.get("warnings", []):
                prefixed = f"[{kb['id']}] {w}"
                if prefixed not in all_warnings:
                    all_warnings.append(prefixed)
        bundle["warnings"] = all_warnings
    return bundle


def write_export_bundle(
    out_path: str | Path,
    kb_specs: list[tuple[str | Path, str | None]],
    *,
    cache_dirs: list[Path] | None = None,
    format_version: int = 2,
    scope: str = "all",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Export KB bundle and write JSON to ``out_path``."""
    bundle = export_bundle(
        kb_specs,
        cache_dirs=cache_dirs,
        format_version=format_version,
        scope=scope,
        generated_at=generated_at,
    )
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    return bundle
