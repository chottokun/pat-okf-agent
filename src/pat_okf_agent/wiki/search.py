"""Search backends for the wiki.

Pattern A ships a deterministic, stdlib-only scorer. The backend interface is
kept deliberately small so a Gemma-embedding + Qdrant adapter can be dropped in
later without touching callers (reversible search adapter).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .model import Concept, iter_concepts, load_concept

__all__ = [
    "BackendUnavailable",
    "LocalSearchBackend",
    "SearchHit",
    "get_backend",
    "pattern_for",
]

_WORD_RE = re.compile(r"[0-9A-Za-z_\u3040-\u30ff\u4e00-\u9fff]+")


class BackendUnavailable(RuntimeError):
    """Raised when an optional backend (Qdrant/Gemma) is not installed."""


@dataclass(frozen=True)
class SearchHit:
    node: str
    score: float
    title: str
    snippet: str
    matched_terms: int

    def as_dict(self) -> dict:
        return {"node": self.node, "score": round(self.score, 3),
                "title": self.title, "snippet": self.snippet,
                "matched_terms": self.matched_terms}


class LocalSearchBackend:
    """Pattern A: scan Markdown, score title/description/tags/body."""

    def __init__(self, root: str | Path, *, use_ripgrep: bool = True):
        self.root = Path(root)
        self.use_ripgrep = use_ripgrep

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        terms = [term.casefold() for term in _WORD_RE.findall(query or "")]
        if not terms or limit <= 0:
            return []
        candidates = self._ripgrep_candidates(terms)
        concepts = iter_concepts(self.root)
        if candidates is not None:
            concepts = [concept for concept in concepts
                        if concept.rel in candidates]
        hits: list[SearchHit] = []
        for concept in concepts:
            hit = _score(concept, terms)
            if hit:
                hits.append(hit)
        hits.sort(key=lambda hit: (-hit.score, -hit.matched_terms, hit.node))
        return hits[:limit]

    def search_keywords(self, query: str, top_k: int = 3) -> list[str]:
        """Driver-style seed discovery used by the ContextResolver pseudocode."""
        return [hit.node for hit in self.search(query, limit=top_k)]

    def _ripgrep_candidates(self, terms: list[str]) -> set[str] | None:
        """Use ripgrep for fast seed discovery, falling back to Python.

        ``None`` means "scan all Markdown files"; an empty set means ripgrep
        ran successfully and found no candidate files.
        """
        if not self.use_ripgrep or shutil.which("rg") is None:
            return None
        args = ["rg", "-i", "-l", "--type", "md", "--no-messages",
                "--glob", "!index*.md", "--glob", "!log.md",
                "--fixed-strings"]
        for term in terms:
            args.extend(["-e", term])
        args.append(".")
        try:
            result = subprocess.run(
                args, cwd=self.root, capture_output=True, text=True,
                timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode not in (0, 1):
            return None
        candidates: set[str] = set()
        for line in result.stdout.splitlines():
            rel = line.strip()
            if rel.startswith("./"):
                rel = rel[2:]
            if rel.endswith(".md"):
                candidates.add(rel)
        return candidates


def _score(concept: Concept, terms: list[str]) -> SearchHit | None:
    title = concept.title
    description = concept.description
    tags = " ".join(concept.tags)
    body = concept.body
    title_l, description_l, tags_l, body_l = (
        title.casefold(), description.casefold(), tags.casefold(),
        body.casefold())

    score = 0.0
    matched = 0
    for term in terms:
        term_score = (
            title_l.count(term) * 6.0
            + description_l.count(term) * 3.0
            + tags_l.count(term) * 3.0
            + body_l.count(term) * 1.0
        )
        if term_score > 0:
            matched += 1
            score += term_score
    if matched == 0:
        return None

    score = score * matched + matched * 2.0
    return SearchHit(
        node=concept.rel,
        score=score,
        title=title,
        snippet=_snippet(body, description, terms),
        matched_terms=matched,
    )


def _snippet(body: str, description: str, terms: list[str]) -> str:
    for line in body.splitlines():
        stripped = " ".join(line.split())
        if not stripped or stripped.startswith("#"):
            continue
        lowered = stripped.casefold()
        if any(term in lowered for term in terms):
            return _clip(stripped, 160)
    if description:
        return _clip(" ".join(description.split()), 160)
    return ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def pattern_for(node_count: int,
                synonym_failure_rate: float | None = None) -> str:
    """Recommend Pattern A, B or C for a wiki and retrieval quality.

    A known synonym-expansion failure rate above 15% is a Pattern C signal even
    for a small wiki; otherwise size thresholds keep the decision predictable.
    """
    if synonym_failure_rate is not None:
        if not 0.0 <= synonym_failure_rate <= 1.0:
            raise ValueError("synonym_failure_rate must be between 0 and 1")
        if synonym_failure_rate > 0.15:
            return "C"
    if node_count <= 500:
        return "A"
    if node_count <= 2000:
        return "B"
    return "C"


def get_backend(name: str, root: str | Path, **kwargs):
    """Create a search backend by name (reversible adapter seam).

    ``auto`` follows the plan's growth trigger: Pattern A below 500 nodes,
    Pattern B (SQLite-FTS5) at 500 or more.  Pattern C is explicit
    (``onnx``/``qdrant``) because it requires optional infrastructure; use
    ``pattern_for(..., synonym_failure_rate=...)`` for the quality signal.
    """
    if name == "auto":
        node_count = kwargs.pop("node_count", None)
        if node_count is None:
            node_count = len(iter_concepts(root))
        name = "sqlite" if node_count > 500 else "local"
    if name == "local":
        kwargs.pop("db_path", None)
        return LocalSearchBackend(root, **kwargs)
    if name == "sqlite":
        from .sqlite_backend import SQLiteSearchBackend
        return SQLiteSearchBackend(root, **kwargs)
    if name == "onnx":
        from .onnx_backend import MicroOnnxBackend
        return MicroOnnxBackend(root, **kwargs)
    if name == "qdrant":
        from .qdrant_backend import QdrantBackend
        return QdrantBackend(root, **kwargs)
    raise ValueError(f"unknown search backend: {name}")
