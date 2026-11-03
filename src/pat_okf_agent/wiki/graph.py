"""Relative-link wiki graph with heading-aware, cycle-safe traversal."""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import FrontmatterError
from .model import (
    SCOPE_WEIGHTS,
    Concept,
    is_special,
    load_concept,
    relevance,
    scope_of,
    tokenize,
)

__all__ = ["Edge", "TraversalHit", "WikiGraph", "build_graph"]


@dataclass(frozen=True)
class Edge:
    """One Markdown link together with the section in which it appears."""

    source: str
    target: str
    heading: str
    scope: str
    anchor: str
    context: str

    def as_dict(self) -> dict:
        return {
            "source_path": self.source,
            "target_path": self.target,
            "heading": self.heading,
            "heading_scope": self.scope,
            "anchor_text": self.anchor,
            "context": self.context,
        }


@dataclass(frozen=True)
class TraversalHit:
    node: str
    score: float
    depth: int
    path: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"node": self.node, "score": round(self.score, 6),
                "depth": self.depth, "path": list(self.path)}


class WikiGraph:
    """Directed graph of concepts, built from relative Markdown links."""

    def __init__(self, nodes: dict[str, Concept]):
        self.nodes = nodes
        self.edges: dict[str, list[str]] = {rel: [] for rel in nodes}
        self.backlinks: dict[str, list[str]] = {rel: [] for rel in nodes}
        self.edge_objects: dict[str, list[Edge]] = {rel: [] for rel in nodes}
        self.backlink_objects: dict[str, list[Edge]] = {
            rel: [] for rel in nodes}
        self.broken: list[tuple[str, str]] = []
        self.parse_errors: list[tuple[str, str]] = []
        self._build()

    def _build(self) -> None:
        for rel, concept in self.nodes.items():
            seen: set[str] = set()
            for link in concept.links:
                if link.external:
                    continue
                if link.resolved is None:
                    self.broken.append((rel, link.target))
                    continue
                target = link.resolved
                if target == rel:
                    continue
                if target not in self.nodes:
                    self.broken.append((rel, target))
                    continue

                # Full-plan rule: weight belongs to the heading section where
                # the link occurs.  Generic sections fall back to the target
                # node's scope for backwards-compatible wiki authoring.
                scope = link.section_scope or scope_of(self.nodes[target].meta)
                edge = Edge(
                    source=rel,
                    target=target,
                    heading=link.heading,
                    scope=scope,
                    anchor=link.text,
                    context=link.context,
                )
                self.edge_objects[rel].append(edge)
                self.backlink_objects[target].append(edge)

                if target in seen:
                    continue
                seen.add(target)
                self.edges[rel].append(target)

        for rel in self.edges:
            self.edges[rel].sort()
        for rel in self.backlinks:
            self.backlinks[rel] = sorted(
                {edge.source for edge in self.backlink_objects[rel]})
        self.broken.sort()

    def node(self, rel: str) -> Concept:
        return self.nodes[rel]

    def resolve_seed(self, seed: str) -> str:
        """Resolve a path, stem or publication id to a node key."""
        raw = seed.strip().replace("\\", "/")
        if raw in self.nodes:
            return raw
        lowered = raw.casefold()
        if raw + ".md" in self.nodes:
            return raw + ".md"
        for rel in self.nodes:
            if rel.casefold() in (lowered, lowered + ".md"):
                return rel
        for rel in self.nodes:
            if Path(rel).stem.casefold() == lowered:
                return rel
        compact = lowered.replace(".", "").replace("-", "").replace(" ", "")
        for rel in sorted(self.nodes):
            if Path(rel).stem.casefold().replace(".", "").replace(
                    "-", "").replace(" ", "") == compact:
                return rel
        raise KeyError(seed)

    def neighbours(self, rel: str, direction: str = "out") -> list[str]:
        if direction == "out":
            return list(self.edges.get(rel, []))
        if direction == "in":
            return list(self.backlinks.get(rel, []))
        return sorted(set(self.edges.get(rel, []))
                      | set(self.backlinks.get(rel, [])))

    def _directed_edges(self, rel: str, direction: str) -> list[Edge]:
        if direction == "out":
            return list(self.edge_objects.get(rel, []))
        if direction == "in":
            return list(self.backlink_objects.get(rel, []))
        return (list(self.edge_objects.get(rel, []))
                + list(self.backlink_objects.get(rel, [])))

    def edge_scores(
        self,
        rel: str,
        *,
        query: str = "",
        direction: str = "out",
        scope_weights: dict[str, float] | None = None,
        scopes: set[str] | None = None,
        min_relevance: float = 0.05,
    ) -> dict[str, float]:
        """Return ``Σ_e W(scope(e)) × Relevance(Q, text(e))`` per target.

        Parallel links are summed as required by the plan.  A small relevance
        floor keeps structurally useful links traversable when an author uses
        generic anchor text; set ``min_relevance=0`` for the strict formula.
        """
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be 'out', 'in' or 'both'")
        if not 0.0 <= min_relevance <= 1.0:
            raise ValueError("min_relevance must be between 0 and 1")
        weights = dict(SCOPE_WEIGHTS)
        if scope_weights:
            weights.update(scope_weights)
        allowed = None
        if scopes is not None:
            allowed = {scope.casefold() for scope in scopes}
            unknown = allowed - set(weights)
            if unknown:
                raise ValueError(
                    f"unknown scope(s): {', '.join(sorted(unknown))}")

        query_tokens = set(tokenize(query))
        scores: dict[str, float] = {}
        for edge in self._directed_edges(rel, direction):
            if allowed is not None and edge.scope not in allowed:
                continue
            match = relevance(query_tokens, edge.context)
            if query_tokens:
                match = max(match, min_relevance)
            weight = weights.get(edge.scope, 1.0)
            neighbour = (edge.target if direction == "out" or
                         direction == "both" and edge.source == rel
                         else edge.source)
            scores[neighbour] = scores.get(neighbour, 0.0) + weight * match
        return scores

    def traverse(
        self,
        seeds: list[str],
        *,
        depth: int = 2,
        gamma: float = 0.85,
        direction: str = "out",
        max_nodes: int = 50,
        scope_weights: dict[str, float] | None = None,
        scopes: set[str] | None = None,
        query: str = "",
        min_relevance: float = 0.05,
    ) -> list[TraversalHit]:
        """Best-first traversal with per-edge heading weights and relevance.

        For each hop::

            S(N_i -> N_j) = Σ_e W(scope(e)) × Relevance(Q, text(e))
            score_child   = score_parent × S × γ

        This is the plan's path-product rule with γ applied per hop.  Nodes
        are finalised once, so cycles terminate deterministically.
        """
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be 'out', 'in' or 'both'")
        if not 0.0 <= gamma <= 1.0:
            raise ValueError("gamma must be between 0 and 1")
        if depth < 0:
            raise ValueError("depth must be >= 0")
        if max_nodes < 1:
            raise ValueError("max_nodes must be >= 1")

        resolved: list[str] = []
        for seed in seeds:
            rel = seed if seed in self.nodes else self.resolve_seed(seed)
            if rel not in resolved:
                resolved.append(rel)
        if not resolved:
            return []

        heap: list[tuple[float, int, str, int, tuple[str, ...]]] = []
        order = 0
        for seed in resolved:
            heapq.heappush(heap, (-1.0, order, seed, 0, (seed,)))
            order += 1

        best: dict[str, TraversalHit] = {}
        visited: set[str] = set()
        while heap:
            neg_score, _, rel, level, path = heapq.heappop(heap)
            if rel in visited:
                continue
            visited.add(rel)
            score = -neg_score
            best[rel] = TraversalHit(rel, score, level, path)
            if level >= depth:
                continue
            for neighbour, edge_score in self.edge_scores(
                    rel, query=query, direction=direction,
                    scope_weights=scope_weights, scopes=scopes,
                    min_relevance=min_relevance).items():
                if neighbour in visited or edge_score <= 0.0:
                    continue
                new_score = score * edge_score * gamma
                heapq.heappush(heap, (-new_score, order, neighbour,
                                      level + 1, path + (neighbour,)))
                order += 1

        hits = sorted(best.values(),
                      key=lambda hit: (-hit.score, hit.depth, hit.path))
        return hits[:max_nodes]


def build_graph(root: str | Path) -> WikiGraph:
    """Load every concept under ``root`` and build its link graph."""
    root = Path(root)
    nodes: dict[str, Concept] = {}
    errors: list[tuple[str, str]] = []
    if root.exists():
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            if is_special(rel):
                continue
            try:
                nodes[rel] = load_concept(path, root)
            except FrontmatterError as exc:
                errors.append((rel, str(exc)))
    graph = WikiGraph(nodes)
    graph.parse_errors = errors
    return graph
