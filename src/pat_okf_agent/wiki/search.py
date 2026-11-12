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
from .tokenize import tokenize

__all__ = [
    "ADMIN_BASENAMES",
    "BackendUnavailable",
    "LocalSearchBackend",
    "SearchHit",
    "get_backend",
    "is_admin_file",
    "pattern_for",
]

ADMIN_BASENAMES = frozenset({
    "buildlog.md",
    "summary.md",
    "index.md",
    "log.md",
})

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{2,6}\s+(.+)$", re.MULTILINE)


def is_admin_file(rel: str) -> bool:
    """Return True if rel is an admin/aggregate note."""
    basename = rel.lower().split("/")[-1]
    return basename in ADMIN_BASENAMES or basename.startswith("index-")


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

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        include_admin: bool = False,
        aux_terms: list[str] | None = None,
        doc_type: str | list[str] | None = None,
        scope: str | list[str] | None = None,
    ) -> list[SearchHit]:
        terms = tokenize(query or "")
        tokenized_aux: list[str] = []
        if aux_terms:
            for item in aux_terms:
                for t in tokenize(item):
                    if t not in terms and t not in tokenized_aux:
                        tokenized_aux.append(t)

        if not terms and not tokenized_aux:
            return []
        if limit <= 0:
            return []

        all_terms = terms + tokenized_aux
        candidates = self._ripgrep_candidates(all_terms, include_admin=include_admin)
        concepts = iter_concepts(self.root)

        if not include_admin:
            concepts = [c for c in concepts if not is_admin_file(c.rel)]
        if doc_type:
            types = {doc_type.lower()} if isinstance(doc_type, str) else {t.lower() for t in doc_type}
            concepts = [c for c in concepts
                        if (str(c.meta.get("type", "")).lower() in types or
                            any(c.rel.lower().startswith(f"{t}s/") or c.rel.lower().startswith(f"{t}/") for t in types))]
        if scope:
            scopes = {scope.lower()} if isinstance(scope, str) else {s.lower() for s in scope}
            concepts = [c for c in concepts if str(c.meta.get("scope", "")).lower() in scopes]

        if candidates is not None:
            concepts = [concept for concept in concepts
                        if concept.rel in candidates]

        hits: list[SearchHit] = []
        for concept in concepts:
            hit = _score(concept, terms, raw_query=query, aux_terms=tokenized_aux)
            if hit:
                hits.append(hit)
        hits.sort(key=lambda hit: (-hit.score, -hit.matched_terms, hit.node))
        return hits[:limit]

    def search_keywords(self, query: str, top_k: int = 3) -> list[str]:
        """Driver-style seed discovery used by the ContextResolver pseudocode."""
        return [hit.node for hit in self.search(query, limit=top_k)]

    def _ripgrep_candidates(
        self,
        terms: list[str],
        *,
        include_admin: bool = False,
    ) -> set[str] | None:
        """Use ripgrep for fast seed discovery, falling back to Python.

        ``None`` means "scan all Markdown files"; an empty set means ripgrep
        ran successfully and found no candidate files.
        """
        if not self.use_ripgrep or shutil.which("rg") is None:
            return None
        args = ["rg", "-i", "-l", "--type", "md", "--no-messages",
                "--glob", "!index*.md", "--glob", "!log.md",
                "--fixed-strings"]
        if not include_admin:
            args.extend([
                "--glob", "!buildlog.md", "--glob", "!BUILDLOG.md",
                "--glob", "!summary.md", "--glob", "!SUMMARY.md",
            ])
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


def _extract_lead(body: str) -> str:
    lines: list[str] = []
    in_lead = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if in_lead:
                break
            continue
        if stripped.startswith("#"):
            if in_lead:
                break
            continue
        in_lead = True
        lines.append(stripped)
    lead = " ".join(lines)
    return lead[:300]


def _phrase_matches(phrase: str, target: str) -> bool:
    if len(phrase) < 2:
        return False
    if phrase in target:
        return True
    norm_phrase = re.sub(r"[-_]+", " ", phrase).strip()
    norm_target = re.sub(r"[-_]+", " ", target)
    return len(norm_phrase) >= 2 and norm_phrase in norm_target


def _score(
    concept: Concept,
    terms: list[str],
    raw_query: str = "",
    aux_terms: list[str] | None = None,
) -> SearchHit | None:
    title = concept.title
    title_l = title.casefold()
    tags_l = " ".join(concept.tags).casefold()
    description = concept.description
    desc_l = description.casefold()

    clean_body = _CODE_BLOCK_RE.sub("", concept.body)
    clean_body_l = clean_body.casefold()

    headings = " ".join(_HEADING_RE.findall(clean_body))
    lead = _extract_lead(clean_body)
    hl_l = f"{headings}\n{lead}".casefold()

    clean_query = raw_query.strip().casefold()
    phrase_bonus = 0.0
    if len(clean_query) >= 2:
        if _phrase_matches(clean_query, title_l):
            phrase_bonus += 50.0
        if _phrase_matches(clean_query, desc_l):
            phrase_bonus += 20.0
        if _phrase_matches(clean_query, hl_l):
            phrase_bonus += 15.0

    title_score = 0.0
    tag_score = 0.0
    desc_score = 0.0
    hl_score = 0.0
    body_score = 0.0

    matched_terms = 0
    matched_aux = 0

    for term in terms:
        term_matched = False
        if term in title_l:
            title_score += 20.0
            term_matched = True
        if any(term in tag.casefold() for tag in concept.tags):
            tag_score += 15.0
            term_matched = True
        if term in desc_l:
            desc_score += 10.0
            term_matched = True
        if term in hl_l:
            hl_score += 5.0
            term_matched = True
        cnt = clean_body_l.count(term)
        if cnt > 0:
            body_score += float(cnt)
            term_matched = True
        if term_matched:
            matched_terms += 1

    if aux_terms:
        for term in aux_terms:
            term_matched = False
            if term in title_l:
                title_score += 10.0
                term_matched = True
            if any(term in tag.casefold() for tag in concept.tags):
                tag_score += 7.5
                term_matched = True
            if term in desc_l:
                desc_score += 5.0
                term_matched = True
            if term in hl_l:
                hl_score += 2.5
                term_matched = True
            cnt = clean_body_l.count(term)
            if cnt > 0:
                body_score += float(cnt) * 0.5
                term_matched = True
            if term_matched:
                matched_aux += 1

    if matched_terms == 0 and matched_aux == 0:
        return None

    # SimpleWiki AND condition for pure ASCII alphanumeric queries (when no aux_terms)
    is_pure_ascii = clean_query.isascii() and bool(re.match(r"^[a-zA-Z0-9_\-\s]+$", clean_query))
    if is_pure_ascii and not aux_terms and len(terms) > 1 and matched_terms < len(terms):
        return None

    # Saturated caps (Owner decision: body cap = 10.0)
    hl_score = min(hl_score, 10.0)
    body_score = min(body_score, 10.0)

    score = phrase_bonus + title_score + tag_score + desc_score + hl_score + body_score

    # Deprecated attenuation
    tags_lower = {t.lower() for t in concept.tags}
    if (
        any(d in tags_lower for d in ("deprecated", "obsolete", "archived"))
        or concept.meta.get("deprecated") is True
    ):
        score *= 0.3

    all_matched = terms if matched_terms > 0 else (aux_terms or [])
    return SearchHit(
        node=concept.rel,
        score=score,
        title=title,
        snippet=_snippet(concept.body, description, all_terms=terms + (aux_terms or [])),
        matched_terms=matched_terms,
    )


def _snippet(body: str, description: str, all_terms: list[str]) -> str:
    for line in body.splitlines():
        stripped = " ".join(line.split())
        if not stripped or stripped.startswith("#"):
            continue
        lowered = stripped.casefold()
        if any(term in lowered for term in all_terms):
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
    raise ValueError(f"unknown backend: {name!r}")
