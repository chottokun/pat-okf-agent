"""Bounded synonym expansion and retrieval-failure evaluation.

The synonym file is versioned Markdown-adjacent configuration (``synonyms.json``
at the wiki root).  Expansion is deterministic and bounded so a query with
several alias groups cannot explode into an unbounded number of backend calls.

``evaluate_synonyms`` runs a small, explicitly labelled query set against the
same backend with and without expansion.  Its failure rate feeds the Pattern C
decision documented in ``docs/okf_wiki.md`` (more than 15% failure).
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .model import Concept, iter_concepts, load_concept
from .search import SearchHit, pattern_for

__all__ = [
    "DEFAULT_EXCLUDE_TAGS",
    "ExpandedSearchBackend",
    "SynonymIndex",
    "classify_tag_tier",
    "evaluate_synonyms",
    "extract_concept_synonym_groups",
    "inventory_wiki_tags",
    "load_eval_queries",
    "pattern_reason",
    "plan_synonyms_sync",
    "sync_synonyms",
]

_SYNONYM_FAILURE_THRESHOLD = 0.15

DEFAULT_EXCLUDE_TAGS: frozenset[str] = frozenset({
    # Broad domain / physical property / category / facet tags
    "semiconductor", "半導体", "パワー半導体", "wide-bandgap",
    "thermal", "thermal-conductivity", "熱伝導率", "高熱伝導",
    "thermal-management", "放熱", "cooling", "liquid-cooling", "two-phase",
    "heat-spreader", "ヒートスプレッダ", "heat-sink",
    "substrate", "放熱基板", "baseplate", "ceramic-substrate",
    "packaging", "package", "power-module",
    "server", "data-center", "aerospace", "high-power",
    "cte", "low-cte", "insulating", "dielectric", "anisotropic",
    "composite", "mmc", "metal-matrix-composite", "ceramics", "セラミックス",
    "filler", "フィラー", "thermal-sheet", "放熱シート",
    "wick", "capillary-wick", "die-attach", "ダイアタッチ",
    "grease", "pad", "graphite", "黒鉛", "cvd", "hemt",
    "materials", "benchmark", "overview", "taxonomy", "measurement",
    # OKF / patent structural tags
    "okf", "epo", "patent", "stub", "example", "synthetic",
    "workflow", "gotcha", "gotchas", "synergistic", "synergy",
    "buildlog", "summary", "provenance-v4",
})


@dataclass(frozen=True)
class SynonymIndex:
    """Validated alias groups loaded from ``synonyms.json``."""

    groups: tuple[tuple[str, ...], ...]
    path: str = ""

    @classmethod
    def from_path(cls, path: str | Path) -> "SynonymIndex":
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{source}: invalid synonyms JSON: {exc.msg}") from exc
        return cls.from_data(data, path=str(source))

    @classmethod
    def from_data(cls, data: Any, *, path: str = "") -> "SynonymIndex":
        if isinstance(data, dict):
            data = data.get("groups")
        if not isinstance(data, list):
            raise ValueError("synonyms must be a list of alias groups")
        groups: list[tuple[str, ...]] = []
        seen: dict[str, str] = {}
        for number, raw_group in enumerate(data, 1):
            if not isinstance(raw_group, list) or len(raw_group) < 2:
                raise ValueError(
                    f"synonym group {number} must be a list of at least 2 "
                    "terms")
            group: list[str] = []
            for raw_term in raw_group:
                if not isinstance(raw_term, str) or not raw_term.strip():
                    raise ValueError(
                        f"synonym group {number} contains an empty term")
                term = " ".join(raw_term.split())
                folded = term.casefold()
                if folded in seen:
                    raise ValueError(
                        f"synonym {term!r} appears in groups "
                        f"{seen[folded]} and {number}")
                seen[folded] = str(number)
                group.append(term)
            groups.append(tuple(group))
        return cls(groups=tuple(groups), path=path)

    def matching_groups(self, query: str) -> list[tuple[str, tuple[str, ...]]]:
        """Return every group with a term occurring in ``query``."""
        matches: list[tuple[str, tuple[str, ...]]] = []
        for group in self.groups:
            for term in group:
                if _term_pattern(term).search(query):
                    matches.append((term, group))
                    break
        return matches

    def expand_query(self, query: str, *, max_variants: int = 8) -> list[str]:
        """Return bounded query variants, preserving the original first."""
        if max_variants < 1:
            raise ValueError("max_variants must be >= 1")
        variants = [query]
        for _term, group in self.matching_groups(query):
            # Prefer the longest matching term and ignore shorter terms that
            # are already part of it, so "amorphous alloy" is not partially
            # replaced as "amorphous" by a shorter alias.
            matched = sorted((term for term in group
                              if _term_pattern(term).search(query)),
                             key=len, reverse=True)
            terms: list[str] = []
            for term in matched:
                if not any(term.casefold() in selected.casefold()
                           for selected in terms):
                    terms.append(term)
            next_variants: list[str] = []
            for variant in variants:
                next_variants.append(variant)
                for term in terms:
                    alternatives = [candidate for candidate in group
                                    if candidate.casefold() != term.casefold()]
                    pattern = _term_pattern(term)
                    for replacement in alternatives:
                        candidate = pattern.sub(replacement, variant)
                        if candidate.casefold() not in {
                                item.casefold() for item in next_variants}:
                            next_variants.append(candidate)
            variants = next_variants[:max_variants]
        if query.casefold() not in {item.casefold() for item in variants}:
            variants = [query] + variants[:max_variants - 1]
        elif variants and variants[0] != query:
            variants = [query] + [item for item in variants
                                  if item.casefold() != query.casefold()]
            variants = variants[:max_variants]
        return variants


class ExpandedSearchBackend:
    """Inline-weighted synonym expansion backend.

    Instead of independent variant searches with max-merge, synonyms are passed
    as auxiliary terms with attenuated weights (0.5x), preventing synonym noise
    from dominating the primary query.
    """

    def __init__(self, backend: Any, index: SynonymIndex, *,
                 max_variants: int = 8):
        self.backend = backend
        self.index = index
        self.max_variants = max_variants

    def search(self, query: str, *, limit: int = 10, **kwargs) -> list[SearchHit]:
        if limit <= 0 or not query.strip():
            return []
        aux_terms: list[str] = []
        for matched_term, group in self.index.matching_groups(query):
            for term in group:
                if (term.casefold() != matched_term.casefold() and
                        term.casefold() not in query.casefold()):
                    if term not in aux_terms:
                        aux_terms.append(term)
        try:
            return self.backend.search(
                query, limit=limit, aux_terms=aux_terms, **kwargs)
        except TypeError:
            # Fallback if backend does not support aux_terms
            return self.backend.search(query, limit=limit, **kwargs)

    def search_keywords(self, query: str, top_k: int = 3, **kwargs) -> list[str]:
        return [hit.node for hit in self.search(query, limit=top_k, **kwargs)]


def load_eval_queries(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate ``eval_queries.json``."""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{source}: invalid query JSON: {exc.msg}") from exc
    if isinstance(data, dict):
        data = data.get("queries")
    if not isinstance(data, list) or not data:
        raise ValueError("eval queries must be a non-empty list")
    queries: list[dict[str, Any]] = []
    for number, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"eval query {number} must be an object")
        query = item.get("query")
        expected = item.get("expected")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"eval query {number} needs a non-empty query")
        if not isinstance(expected, list) or not expected or not all(
                isinstance(node, str) and node.strip() for node in expected):
            raise ValueError(
                f"eval query {number} needs a non-empty expected path list")
        queries.append({
            "id": str(item.get("id") or number),
            "query": query.strip(),
            "expected": [node.strip().lstrip("./") for node in expected],
        })
    return queries


def evaluate_synonyms(root: str | Path, backend: Any, index: SynonymIndex, *,
                      queries: Iterable[dict[str, Any]], limit: int = 5,
                      failure_threshold: float = _SYNONYM_FAILURE_THRESHOLD
                      ) -> dict[str, Any]:
    """Compare baseline and expanded retrieval over labelled queries."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if not 0.0 <= failure_threshold <= 1.0:
        raise ValueError("failure_threshold must be between 0 and 1")
    expanded = ExpandedSearchBackend(backend, index)
    rows: list[dict[str, Any]] = []
    baseline_failures = 0
    failures = 0
    improved = regressed = 0
    for item in queries:
        expected = set(item["expected"])
        baseline_hits = backend.search(item["query"], limit=limit)
        expanded_hits = expanded.search(item["query"], limit=limit)
        baseline_ok = bool(expected & {hit.node for hit in baseline_hits})
        expanded_ok = bool(expected & {hit.node for hit in expanded_hits})
        baseline_failures += int(not baseline_ok)
        failures += int(not expanded_ok)
        improved += int(expanded_ok and not baseline_ok)
        regressed += int(baseline_ok and not expanded_ok)
        rows.append({
            "id": item.get("id"),
            "query": item["query"],
            "expected": sorted(expected),
            "baseline_nodes": [hit.node for hit in baseline_hits],
            "expanded_nodes": [hit.node for hit in expanded_hits],
            "baseline_ok": baseline_ok,
            "expanded_ok": expanded_ok,
            "variants": index.expand_query(item["query"]),
        })
    if not rows:
        raise ValueError("at least one evaluation query is required")
    total = len(rows)
    failure_rate = failures / total
    baseline_rate = baseline_failures / total
    node_count = len(iter_concepts(root))
    return {
        "total": total,
        "limit": limit,
        "baseline_failures": baseline_failures,
        "baseline_failure_rate": round(baseline_rate, 6),
        "failures": failures,
        "failure_rate": round(failure_rate, 6),
        "improved": improved,
        "regressed": regressed,
        "failure_threshold": failure_threshold,
        "recommended_pattern": pattern_for(node_count, failure_rate),
        "pattern_reason": pattern_reason(node_count, failure_rate),
        "rows": rows,
    }


def _term_pattern(term: str) -> re.Pattern[str]:
    """Match Latin aliases on word boundaries and CJK aliases as substrings."""
    escaped = re.escape(term)
    if re.fullmatch(r"[0-9A-Za-z](?:[0-9A-Za-z ._-]*[0-9A-Za-z])?", term):
        return re.compile(
            rf"(?<![0-9A-Za-z]){escaped}(?![0-9A-Za-z])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def pattern_reason(node_count: int, synonym_failure_rate: float | None) -> str:
    if node_count > 2000:
        return "node count is above 2000"
    if (synonym_failure_rate is not None
            and synonym_failure_rate > _SYNONYM_FAILURE_THRESHOLD):
        return "synonym failure rate is above 15%"
    if node_count > 500:
        return "node count is above 500"
    return "node count and synonym failure rate are below Pattern B/C triggers"


def _target_concepts_for_synonyms(root: Path) -> list[Concept]:
    all_concepts = [
        c for c in iter_concepts(root)
        if "stub" not in {t.strip().casefold() for t in c.tags}
    ]
    has_concepts_dir = any(c.rel.startswith("concepts/") for c in all_concepts)
    if has_concepts_dir:
        return [
            c for c in all_concepts
            if c.rel.startswith("concepts/")
            and (not c.type or c.type.strip().casefold() == "concept")
        ]
    return [
        c for c in all_concepts
        if c.type.strip().casefold() == "concept"
    ]


def _slug_initials(slug: str) -> set[str]:
    parts = [p for p in slug.casefold().split("-") if p]
    if len(parts) < 2:
        return set()
    return {"".join(p[0] for p in parts)}


def _resolve_shared_tag_owner(
    cf: str,
    holders: list[tuple[str, str, str]],
) -> str | None:
    if len(holders) == 1:
        return holders[0][0]
    # 1. Exact slug or singular/plural slug match
    exact = [
        s for s, _, _ in holders
        if s.casefold() in (cf, cf + "s", cf.rstrip("s"))
    ]
    if len(exact) == 1:
        return exact[0]
    # 2. Initialism of multi-word slug (e.g. TIM -> thermal-interface-materials)
    initials = [s for s, _, _ in holders if cf in _slug_initials(s)]
    if len(initials) == 1:
        return initials[0]
    # 3. Slug starts with "<tag>-"
    slug_prefix = [
        s for s, _, _ in holders
        if s.casefold().startswith(cf + "-")
    ]
    if len(slug_prefix) == 1:
        return slug_prefix[0]
    # 4. Tag appears as token/suffix in slug AND in title
    cf_spaced = cf.replace("-", " ")
    slug_and_title = [
        s for s, title, _ in holders
        if (cf in s.casefold().split("-") or s.casefold().endswith("-" + cf))
        and cf_spaced in title.casefold().replace("-", " ")
    ]
    if len(slug_and_title) == 1:
        return slug_and_title[0]
    # 5. Tag appears in title of only one holder
    in_title = [
        s for s, title, _ in holders
        if cf_spaced in title.casefold().replace("-", " ")
    ]
    if len(in_title) == 1:
        return in_title[0]
    # 6. Title starts with the tag
    title_starts = [
        s for s, title, _ in holders
        if title.casefold().startswith(cf)
    ]
    if len(title_starts) == 1:
        return title_starts[0]
    return None


def extract_concept_synonym_groups(
    root: str | Path,
    *,
    exclude_tags: Iterable[str] | None = None,
) -> list[list[str]]:
    """Extract deterministic synonym groups from concept notes in ``root``."""
    root_path = Path(root)
    excluded = {t.strip().casefold() for t in DEFAULT_EXCLUDE_TAGS}
    if exclude_tags is not None:
        excluded |= {
            " ".join(str(t).split()).casefold()
            for t in exclude_tags
            if str(t).strip()
        }

    concepts = _target_concepts_for_synonyms(root_path)
    concept_rows: list[tuple[str, str, list[str]]] = []
    tag_holders: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    for concept in concepts:
        slug = concept.path.stem.strip()
        title = concept.title
        norm_tags = [
            " ".join(str(t).split())
            for t in concept.tags
            if isinstance(t, str) and str(t).strip()
        ]
        concept_rows.append((slug, title, norm_tags))
        seen_in_concept: set[str] = set()
        for candidate in [slug, *norm_tags]:
            cf = candidate.casefold()
            if not candidate or cf in excluded or cf in seen_in_concept:
                continue
            seen_in_concept.add(cf)
            tag_holders[cf].append((slug, title, candidate))

    owner_of: dict[str, str] = {}
    for cf, holders in tag_holders.items():
        owner = _resolve_shared_tag_owner(cf, holders)
        if owner is not None:
            owner_of[cf] = owner

    groups: list[list[str]] = []
    for slug, _title, norm_tags in concept_rows:
        tag_casing_map: dict[str, str] = {}
        for t in norm_tags:
            if t.casefold() not in tag_casing_map:
                tag_casing_map[t.casefold()] = t
        first_term = tag_casing_map.get(slug.casefold(), slug)
        candidates = [first_term, *norm_tags]
        group: list[str] = []
        seen: set[str] = set()
        for raw_term in candidates:
            term = " ".join(raw_term.split())
            cf = term.casefold()
            if not term or cf in seen or cf in excluded:
                continue
            if owner_of.get(cf) == slug:
                seen.add(cf)
                group.append(term)
        if len(group) >= 2:
            groups.append(group)
    return groups


def _normalize_group_list(raw_groups: Any, *, label: str) -> list[list[str]]:
    if not isinstance(raw_groups, list):
        raise ValueError(f"{label} must be a list of alias groups")
    normalized: list[list[str]] = []
    seen_global: set[str] = set()
    for number, raw_group in enumerate(raw_groups, 1):
        if not isinstance(raw_group, list) or len(raw_group) < 2:
            raise ValueError(
                f"{label} group {number} must be a list of at least 2 terms"
            )
        group: list[str] = []
        for raw_term in raw_group:
            if not isinstance(raw_term, str) or not raw_term.strip():
                raise ValueError(
                    f"{label} group {number} contains an empty term"
                )
            term = " ".join(raw_term.split())
            cf = term.casefold()
            if cf in seen_global:
                raise ValueError(
                    f"duplicate term {term!r} in {label}"
                )
            seen_global.add(cf)
            group.append(term)
        normalized.append(group)
    return normalized


def _load_existing_manual_groups(synonyms_path: Path) -> list[list[str]]:
    if not synonyms_path.exists():
        return []
    try:
        data = json.loads(synonyms_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{synonyms_path}: invalid synonyms JSON: {exc.msg}"
        ) from exc

    if isinstance(data, list):
        # Legacy hand-written array format -> migrate to manual_groups
        return _normalize_group_list(data, label="manual_groups")
    if isinstance(data, dict):
        if "manual_groups" in data or "generated_groups" in data:
            raw_manual = data.get("manual_groups", [])
            return _normalize_group_list(raw_manual, label="manual_groups")
        if "groups" in data:
            # Legacy dict format without generated_groups -> migrate to manual_groups
            return _normalize_group_list(data["groups"], label="manual_groups")
    raise ValueError("synonyms must be a list of alias groups or a sync object")


def _load_existing_tier_a_tags(synonyms_path: Path) -> list[str] | None:
    if not synonyms_path.exists():
        return None
    try:
        data = json.loads(synonyms_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "tier_a_tags" in data:
        raw = data.get("tier_a_tags", [])
        if isinstance(raw, list):
            result: list[str] = []
            seen: set[str] = set()
            for item in raw:
                if isinstance(item, str) and item.strip():
                    term = item.strip()
                    cf = term.casefold()
                    if cf not in seen:
                        seen.add(cf)
                        result.append(term)
            return result
        return []
    return None


def _merge_manual_and_generated_groups(
    manual_groups: list[list[str]],
    generated_groups: list[list[str]],
) -> list[list[str]]:
    merged: list[list[str]] = [list(g) for g in manual_groups]
    term_to_group_idx: dict[str, int] = {}
    for idx, group in enumerate(merged):
        for term in group:
            term_to_group_idx[term.casefold()] = idx

    for gen_group in generated_groups:
        matching_indices = sorted({
            term_to_group_idx[term.casefold()]
            for term in gen_group
            if term.casefold() in term_to_group_idx
        })
        if not matching_indices:
            new_idx = len(merged)
            merged.append(list(gen_group))
            for term in gen_group:
                term_to_group_idx[term.casefold()] = new_idx
        elif len(matching_indices) == 1:
            target_idx = matching_indices[0]
            for term in gen_group:
                cf = term.casefold()
                if cf not in term_to_group_idx:
                    merged[target_idx].append(term)
                    term_to_group_idx[cf] = target_idx
        else:
            remaining = [
                term for term in gen_group
                if term.casefold() not in term_to_group_idx
            ]
            if len(remaining) >= 2:
                new_idx = len(merged)
                merged.append(remaining)
                for term in remaining:
                    term_to_group_idx[term.casefold()] = new_idx

    return merged


def plan_synonyms_sync(
    root: str | Path,
    *,
    synonyms_path: str | Path | None = None,
    exclude_tags: Iterable[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Build the deterministic ``synonyms.json`` payload and serialized text."""
    root_path = Path(root)
    target = (
        Path(synonyms_path)
        if synonyms_path is not None
        else root_path / "synonyms.json"
    )
    manual_groups = _load_existing_manual_groups(target)
    tier_a_tags = _load_existing_tier_a_tags(target)
    generated_groups = extract_concept_synonym_groups(
        root_path, exclude_tags=exclude_tags
    )
    groups = _merge_manual_and_generated_groups(
        manual_groups, generated_groups
    )
    payload: dict[str, Any] = {
        "groups": groups,
        "generated_groups": generated_groups,
        "manual_groups": manual_groups,
    }
    if tier_a_tags is not None:
        payload["tier_a_tags"] = tier_a_tags
    if groups:
        SynonymIndex.from_data(payload, path=str(target))
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return payload, content


def sync_synonyms(
    root: str | Path,
    *,
    synonyms_path: str | Path | None = None,
    exclude_tags: Iterable[str] | None = None,
    check: bool = False,
) -> dict[str, Any]:
    """Synchronize or check ``<wiki>/synonyms.json`` deterministically."""
    root_path = Path(root)
    target = (
        Path(synonyms_path)
        if synonyms_path is not None
        else root_path / "synonyms.json"
    )
    payload, content = plan_synonyms_sync(
        root_path, synonyms_path=target, exclude_tags=exclude_tags
    )
    existing_content: str | None = None
    if target.exists():
        existing_content = target.read_text(encoding="utf-8")
    stale = existing_content != content
    written = False
    if not check and stale:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
        written = True

    return {
        "ok": not (check and stale),
        "path": str(target),
        "written": written,
        "stale": stale if check else False,
        "group_count": len(payload["groups"]),
        "generated_count": len(payload["generated_groups"]),
        "manual_count": len(payload["manual_groups"]),
    }


def classify_tag_tier(
    tag: str,
    *,
    manual_groups: Iterable[Iterable[str]] | None = None,
    tier_a_list: Iterable[str] | None = None,
    concept_slug: str | None = None,
    exclude_tags: Iterable[str] | None = None,
) -> str | None:
    """Classify a tag into None (Tier 外 / excluded), 'Tier A', or 'Tier B'."""
    norm = tag.strip().casefold()
    excludes = frozenset(
        x.casefold() for x in (exclude_tags if exclude_tags is not None else DEFAULT_EXCLUDE_TAGS)
    )
    if norm in excludes:
        return None
    if concept_slug is not None and norm == concept_slug.strip().casefold():
        return "Tier A"
    if tier_a_list is not None:
        tier_a_cf = {x.strip().casefold() for x in tier_a_list if isinstance(x, str)}
        if norm in tier_a_cf:
            return "Tier A"
    if manual_groups is not None:
        manual_cf = {
            x.strip().casefold()
            for grp in manual_groups
            for x in grp
            if isinstance(x, str)
        }
        if norm in manual_cf:
            return "Tier A"
    return "Tier B"


def inventory_wiki_tags(
    root: str | Path,
    *,
    exclude_tags: Iterable[str] | None = None,
    synonyms_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inventory and classify all concept tags across the wiki."""
    root_path = Path(root)
    concepts_dir = root_path / "concepts"
    target_syn = (
        Path(synonyms_path)
        if synonyms_path is not None
        else root_path / "synonyms.json"
    )
    manual_groups = _load_existing_manual_groups(target_syn) if target_syn.exists() else []
    tier_a_tags = _load_existing_tier_a_tags(target_syn) if target_syn.exists() else []
    if tier_a_tags is None:
        tier_a_tags = []

    concept_files = sorted(
        p for p in concepts_dir.glob("*.md")
        if p.name != "index.md" and p.is_file()
    ) if concepts_dir.exists() else []

    tag_frequencies: dict[str, int] = defaultdict(int)
    tags_by_tier: dict[str, set[str]] = {
        "Tier A": set(),
        "Tier B": set(),
        "Tier 外": set(),
    }
    total_tags = 0

    for path in concept_files:
        slug = path.stem
        try:
            concept = load_concept(path, root_path)
            tags = concept.tags
        except Exception:
            continue
        for raw_tag in tags:
            tag = str(raw_tag).strip()
            if not tag:
                continue
            total_tags += 1
            tag_frequencies[tag] += 1
            tier = classify_tag_tier(
                tag,
                manual_groups=manual_groups,
                tier_a_list=tier_a_tags,
                concept_slug=slug,
                exclude_tags=exclude_tags,
            )
            if tier is None:
                tags_by_tier["Tier 外"].add(tag)
            else:
                tags_by_tier[tier].add(tag)

    return {
        "total_concepts": len(concept_files),
        "total_tags": total_tags,
        "unique_tags": len(tag_frequencies),
        "tag_frequencies": dict(tag_frequencies),
        "tier_counts": {
            "Tier A": len(tags_by_tier["Tier A"]),
            "Tier B": len(tags_by_tier["Tier B"]),
            "Tier 外": len(tags_by_tier["Tier 外"]),
        },
        "tags_by_tier": {
            "Tier A": sorted(tags_by_tier["Tier A"]),
            "Tier B": sorted(tags_by_tier["Tier B"]),
            "Tier 外": sorted(tags_by_tier["Tier 外"]),
        },
    }
