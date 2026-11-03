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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .model import iter_concepts
from .search import SearchHit, pattern_for

__all__ = [
    "ExpandedSearchBackend",
    "SynonymIndex",
    "evaluate_synonyms",
    "load_eval_queries",
    "pattern_reason",
]

_SYNONYM_FAILURE_THRESHOLD = 0.15


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
    """Merge synonym-expanded searches using the best score per node."""

    def __init__(self, backend: Any, index: SynonymIndex, *,
                 max_variants: int = 8):
        self.backend = backend
        self.index = index
        self.max_variants = max_variants

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        if limit <= 0 or not query.strip():
            return []
        variants = self.index.expand_query(
            query, max_variants=self.max_variants)
        best: dict[str, SearchHit] = {}
        for variant in variants:
            for hit in self.backend.search(
                    variant, limit=max(limit, min(limit * 2, 100))):
                previous = best.get(hit.node)
                if previous is None or hit.score > previous.score:
                    best[hit.node] = hit
        hits = sorted(best.values(), key=lambda hit: (-hit.score, hit.node))
        return hits[:limit]

    def search_keywords(self, query: str, top_k: int = 3) -> list[str]:
        return [hit.node for hit in self.search(query, limit=top_k)]


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
