"""Context resolution: search seeds, weighted traversal, Gotchas pruning.

This module implements the plan's ``ContextResolver`` while remaining
deterministic and stdlib-only.  It is the semantic-search replacement used by
``okf context`` and can be driven by any search backend exposing ``search``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .graph import WikiGraph, build_graph
from .model import (
    Concept,
    extract_sections,
    iter_concepts,
    relevance,
    tokenize,
)
from .search import LocalSearchBackend, SearchHit

__all__ = [
    "ContextBundle",
    "ContextNode",
    "ContextResolver",
    "clip_to_tokens",
    "estimate_tokens",
    "has_constraint_violation",
]

# Progressive-disclosure budgets from the implementation plan.
PHASE_TOKEN_BUDGETS: dict[str, int] = {
    "phase1": 1000,
    "phase2": 2500,
    "phase3": 4000,
}

DEFAULT_CONSTRAINT_MARKERS = ("INCOMPATIBLE_EXPLICIT",)
_CONSTRAINT_RE = re.compile(
    r"\b(?:INCOMPATIBLE_WITH|NOT_FOR)\s*:\s*([^\n]+)", re.IGNORECASE)
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def estimate_tokens(text: str) -> int:
    """Conservative, dependency-free token estimate.

    Latin words count as one token and CJK characters as one token each.  This
    intentionally over-estimates Markdown prose so token budgets stay strict.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text
              if "\u3040" <= char <= "\u30ff"
              or "\u3400" <= char <= "\u4dbf"
              or "\u4e00" <= char <= "\u9fff")
    words = _LATIN_WORD_RE.findall(text)
    latin_chars = sum(len(word) for word in words)
    latin_tokens = max(len(words), (latin_chars + 3) // 4)
    return max(1, latin_tokens + cjk)


def clip_to_tokens(text: str, limit: int) -> tuple[str, bool]:
    """Clip ``text`` so its estimated token count does not exceed ``limit``."""
    if limit <= 0:
        return "", bool(text)
    if estimate_tokens(text) <= limit:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    clipped = text[:low].rstrip()
    if clipped:
        clipped += "…"
    return clipped, True


def has_constraint_violation(
    query: str,
    gotchas_text: str,
    *,
    markers: tuple[str, ...] | list[str] = DEFAULT_CONSTRAINT_MARKERS,
) -> bool:
    """Return True when a node's Gotchas block excludes the current task.

    ``INCOMPATIBLE_EXPLICIT`` is an unconditional hard stop.  Soft constraints
    use ``INCOMPATIBLE_WITH: windows, gpu`` or ``NOT_FOR: production`` and are
    triggered when a listed term occurs in the query.
    """
    if not gotchas_text:
        return False
    if any(marker and marker in gotchas_text for marker in markers):
        return True

    query_l = query.casefold()
    query_terms = set(tokenize(query))
    for match in _CONSTRAINT_RE.finditer(gotchas_text):
        raw = match.group(1)
        for part in re.split(r"[,;、/]|\band\b", raw):
            constraint = part.strip().strip(".。")
            if not constraint:
                continue
            constraint_l = constraint.casefold()
            if constraint_l in query_l or (
                    set(tokenize(constraint_l)) & query_terms):
                return True
    return False


@dataclass(frozen=True)
class ContextNode:
    node: str
    score: float
    depth: int
    title: str
    text: str

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "score": round(self.score, 6),
            "depth": self.depth,
            "title": self.title,
        }


@dataclass(frozen=True)
class ContextBundle:
    query: str
    seeds: tuple[str, ...]
    nodes: tuple[ContextNode, ...]
    text: str
    tokens: int
    token_limit: int
    pruned: tuple[str, ...] = ()
    truncated: bool = False

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "seeds": list(self.seeds),
            "nodes": [node.as_dict() for node in self.nodes],
            "pruned": list(self.pruned),
            "text": self.text,
            "tokens": self.tokens,
            "token_limit": self.token_limit,
            "truncated": self.truncated,
        }


class ContextResolver:
    """Deterministic context assembly with heading weights and Gotchas pruning."""

    def __init__(
        self,
        root: str | Path,
        *,
        backend=None,
        graph: WikiGraph | None = None,
        scope_weights: dict[str, float] | None = None,
        damping_factor: float = 0.85,
        token_limit: int = PHASE_TOKEN_BUDGETS["phase3"],
        min_relevance: float = 0.05,
        constraint_markers: tuple[str, ...] | list[str] = (
            DEFAULT_CONSTRAINT_MARKERS),
    ):
        self.root = Path(root)
        self.backend = backend or LocalSearchBackend(self.root)
        self.graph = graph or build_graph(self.root)
        self.weights = scope_weights
        self.gamma = damping_factor
        self.token_limit = token_limit
        self.min_relevance = min_relevance
        self.constraint_markers = tuple(constraint_markers)

    def _gotchas_text(self, concept: Concept) -> str:
        sections = [section for section in extract_sections(concept.body)
                    if section.scope == "gotchas"]
        if sections:
            return "\n".join(section.text for section in sections)
        return concept.body if concept.scope == "gotchas" else ""

    def resolve(
        self,
        query: str,
        *,
        max_nodes: int = 5,
        max_seeds: int = 3,
        depth: int = 2,
        token_limit: int | None = None,
    ) -> ContextBundle:
        """Resolve a query into ranked nodes and a token-bounded context text."""
        if max_nodes < 1:
            raise ValueError("max_nodes must be >= 1")
        limit = self.token_limit if token_limit is None else token_limit
        if limit < 1:
            raise ValueError("token_limit must be >= 1")

        seed_hits: list[SearchHit] = self.backend.search(
            query, limit=max_seeds) if query.strip() else []
        seeds = tuple(hit.node for hit in seed_hits)
        if not seeds:
            return ContextBundle(query=query, seeds=(), nodes=(), text="",
                                 tokens=0, token_limit=limit)

        hits = self.graph.traverse(
            list(seeds), depth=depth, gamma=self.gamma,
            query=query, min_relevance=self.min_relevance,
            scope_weights=self.weights, max_nodes=max(max_nodes * 4, 20))

        ranked: list[ContextNode] = []
        pruned: list[str] = []
        for hit in hits:
            concept = self.graph.nodes[hit.node]
            gotchas = self._gotchas_text(concept)
            if has_constraint_violation(
                    query, gotchas, markers=self.constraint_markers):
                pruned.append(hit.node)
                continue
            ranked.append(ContextNode(
                node=hit.node, score=hit.score, depth=hit.depth,
                title=concept.title, text=concept.body))
            if len(ranked) >= max_nodes:
                break

        text_parts: list[str] = []
        used = 0
        kept: list[ContextNode] = []
        truncated = False
        for node in ranked:
            body = node.text.strip()
            first_line = body.splitlines()[0] if body else ""
            if first_line.startswith("# "):
                body = "\n".join(body.splitlines()[1:]).lstrip()
            fragment = f"# {node.title}\n\n{body}\n"
            separator = "\n---\n\n" if text_parts else ""
            candidate = separator + fragment
            cost = estimate_tokens(candidate)
            if used + cost <= limit:
                text_parts.append(candidate)
                used += cost
                kept.append(node)
                continue
            remaining = limit - used - estimate_tokens(separator)
            if remaining > 1:
                clipped, was_clipped = clip_to_tokens(fragment, remaining)
                if clipped:
                    text_parts.append(separator + clipped)
                    used += estimate_tokens(separator + clipped)
                    truncated = truncated or was_clipped
            truncated = True
            break

        text = "".join(text_parts).strip()
        return ContextBundle(
            query=query,
            seeds=seeds,
            nodes=tuple(kept),
            text=text,
            tokens=estimate_tokens(text),
            token_limit=limit,
            pruned=tuple(pruned),
            truncated=truncated,
        )

    # Name used by the implementation-plan pseudocode.
    resolve_context = resolve

    def progressive(self, query: str, *, max_nodes: int = 5) -> dict[str, dict]:
        """Build the three progressive-disclosure phases under their budgets."""
        phase1_text, phase1_cut = clip_to_tokens(
            self._phase1_text(), PHASE_TOKEN_BUDGETS["phase1"])
        phase2_text, phase2_cut = clip_to_tokens(
            self._phase2_text(query), PHASE_TOKEN_BUDGETS["phase2"])
        bundle = self.resolve(
            query, max_nodes=max_nodes,
            token_limit=PHASE_TOKEN_BUDGETS["phase3"])
        return {
            "phase1": {
                "text": phase1_text,
                "tokens": estimate_tokens(phase1_text),
                "token_limit": PHASE_TOKEN_BUDGETS["phase1"],
                "truncated": phase1_cut,
            },
            "phase2": {
                "text": phase2_text,
                "tokens": estimate_tokens(phase2_text),
                "token_limit": PHASE_TOKEN_BUDGETS["phase2"],
                "truncated": phase2_cut,
            },
            "phase3": {
                **bundle.as_dict(),
                "token_limit": PHASE_TOKEN_BUDGETS["phase3"],
            },
        }

    def _phase1_text(self) -> str:
        index = self.root / "index.md"
        if index.exists():
            return index.read_text(encoding="utf-8")
        return "# Knowledge Index\n\n" + "\n".join(
            f"- {concept.rel}: {concept.title}"
            for concept in iter_concepts(self.root))

    def _phase2_text(self, query: str) -> str:
        hits = self.backend.search(query, limit=10)
        lines = ["# Candidate nodes"]
        for hit in hits:
            concept = self.graph.nodes.get(hit.node)
            if concept is None:
                continue
            lines.extend([
                "",
                f"## {hit.node}",
                f"- type: {concept.type}",
                f"- title: {concept.title}",
                f"- description: {concept.description}",
                "- headings:",
            ])
            for section in extract_sections(concept.body):
                if section.heading:
                    lines.append(f"  - {'#' * section.level} {section.heading}")
        return "\n".join(lines)
