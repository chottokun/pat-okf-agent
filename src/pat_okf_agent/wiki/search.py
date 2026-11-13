"""Search backends for the wiki.

Pattern A ships a deterministic, stdlib-only scorer. The backend interface is
kept deliberately small so a Gemma-embedding + Qdrant adapter can be dropped in
later without touching callers (reversible search adapter).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .model import Concept, iter_concepts, load_concept
from .tokenize import tokenize

__all__ = [
    "ADMIN_BASENAMES",
    "BackendUnavailable",
    "LocalSearchBackend",
    "SearchHit",
    "attach_link_counts",
    "get_backend",
    "is_admin_file",
    "pattern_for",
    "strip_suffix",
]

ADMIN_BASENAMES = frozenset({
    "buildlog.md",
    "summary.md",
    "index.md",
    "log.md",
})

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{2,6}\s+(.+)$", re.MULTILINE)


def strip_suffix(word: str) -> str | None:
    """Normalize inflectional suffixes (-s, -es, -ed, -ing) with stem length >= 4 guard.

    Explicitly excludes -er to prevent destroying nouns like solder, copper, spreader.
    """
    if not word or not word.isascii() or not word.isalnum():
        return None
    w = word.casefold()
    if w.endswith("ing"):
        stem = w[:-3]
        if len(stem) >= 4:
            return stem
    elif w.endswith("ed"):
        stem = w[:-2]
        if len(stem) >= 4:
            return stem
    elif w.endswith("es"):
        if len(w) >= 6 and (
            w.endswith("shes") or w.endswith("ches") or w.endswith("xes") or w.endswith("sses") or w.endswith("zes")
        ):
            stem = w[:-2]
            if len(stem) >= 4:
                return stem
        stem = w[:-1]
        if len(stem) >= 4:
            return stem
    elif w.endswith("s") and not w.endswith("ss"):
        stem = w[:-1]
        if len(stem) >= 4:
            return stem
    return None


def _field_has_stem(text: str, stem: str) -> bool:
    """Check if any word in text matches stem with regular inflectional suffixes."""
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        w = m.group(0).casefold()
        if w.startswith(stem):
            rest = w[len(stem):]
            if rest in ("", "s", "es", "ed", "ing"):
                return True
        elif strip_suffix(w) == stem:
            return True
    return False


def _count_stem_in_body(text: str, stem: str) -> int:
    """Count occurrences of words matching stem with regular inflectional suffixes in body."""
    cnt = 0
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        w = m.group(0).casefold()
        if w.startswith(stem) and w[len(stem):] in ("", "s", "es", "ed", "ing"):
            cnt += 1
        elif strip_suffix(w) == stem:
            cnt += 1
    return cnt


def _field_has_wildcard(text: str, prefix: str) -> bool:
    """Check if any word in text starts with prefix."""
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        if m.group(0).casefold().startswith(prefix):
            return True
    return False


def _count_wildcard_in_body(text: str, prefix: str) -> int:
    """Count occurrences of words starting with prefix in body."""
    cnt = 0
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        if m.group(0).casefold().startswith(prefix):
            cnt += 1
    return cnt


def is_admin_file(rel: str) -> bool:
    """Return True if rel is an admin/aggregate note."""
    basename = rel.lower().split("/")[-1]
    return basename in ADMIN_BASENAMES or basename.startswith("index-")


class BackendUnavailable(RuntimeError):
    """Raised when an optional backend (Qdrant/Gemma) is not installed."""


def _round_floats(obj: Any, ndigits: int = 3) -> Any:
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


@dataclass(frozen=True)
class SearchHit:
    node: str
    score: float
    title: str
    snippet: str
    matched_terms: int
    is_fallback: bool = False
    query_corrections: dict[str, str] | None = None
    breakdown: dict | None = None
    outgoing_links: int | None = None
    backlinks: int | None = None

    def as_dict(self) -> dict:
        d = {
            "node": self.node,
            "score": round(self.score, 3),
            "title": self.title,
            "snippet": self.snippet,
            "matched_terms": self.matched_terms,
            "is_fallback": self.is_fallback,
        }
        if self.query_corrections is not None:
            d["query_corrections"] = self.query_corrections
        if self.breakdown is not None:
            bd = _round_floats(self.breakdown, 3)
            bd["total"] = d["score"]
            d["breakdown"] = bd
        if self.outgoing_links is not None:
            d["outgoing_links"] = self.outgoing_links
        if self.backlinks is not None:
            d["backlinks"] = self.backlinks
        return d


def attach_link_counts(hits: list[SearchHit], root: str | Path) -> list[SearchHit]:
    """Attach graph link counts to search hits without altering ranking or score.

    Link counts are informational properties of the whole KB:
    - outgoing_links: number of distinct notes in the KB that this note links to
    - backlinks: number of distinct notes in the KB that link to this note
    Hits whose node is absent from the graph retain None (omitted in as_dict()).
    """
    from .graph import build_graph

    graph = build_graph(root)
    result: list[SearchHit] = []
    for hit in hits:
        if hit.node in graph.nodes:
            result.append(
                replace(
                    hit,
                    outgoing_links=len(graph.edges.get(hit.node, [])),
                    backlinks=len(graph.backlinks.get(hit.node, [])),
                )
            )
        else:
            result.append(hit)
    return result


class LocalSearchBackend:
    """Pattern A: scan Markdown, score title/description/tags/body."""

    def __init__(self, root: str | Path, *, use_ripgrep: bool = True):
        self.root = Path(root)
        self.use_ripgrep = use_ripgrep
        self._vocab: set[str] | None = None

    def _get_vocab(self) -> set[str]:
        if self._vocab is None:
            from .vocab import build_vocab
            self._vocab = build_vocab(self.root)
        return self._vocab

    def _execute_search(
        self,
        query: str,
        *,
        terms: list[str],
        aux_terms: list[str] | None = None,
        limit: int = 10,
        include_admin: bool = False,
        doc_type: str | list[str] | None = None,
        scope: str | list[str] | None = None,
        is_fallback: bool = False,
        query_corrections: dict[str, str] | None = None,
        relaxed_and: bool = False,
        raw_phrase_override: str | None = None,
        wildcards: set[str] | None = None,
    ) -> list[SearchHit]:
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
        rg_terms = list(all_terms)
        for t in all_terms:
            st = strip_suffix(t)
            if st and st not in rg_terms:
                rg_terms.append(st)
        candidates = self._ripgrep_candidates(rg_terms, include_admin=include_admin)
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
            hit = _score(
                concept,
                terms,
                raw_query=query,
                aux_terms=tokenized_aux,
                is_fallback=is_fallback,
                query_corrections=query_corrections,
                relaxed_and=relaxed_and,
                raw_phrase_override=raw_phrase_override,
                wildcards=wildcards,
            )
            if hit:
                hits.append(hit)
        hits.sort(key=lambda hit: (-hit.score, -hit.matched_terms, hit.node))
        return hits[:limit]

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
        if not query:
            return []

        # 1. Boolean OR operator (disjunctive normal form clauses)
        if re.search(r"\bOR\b", query):
            or_parts = [p.strip() for p in re.split(r"\bOR\b", query) if p.strip()]
            if len(or_parts) > 1:
                combined_hits: dict[str, SearchHit] = {}
                for part in or_parts:
                    part_hits = self.search(
                        part,
                        limit=limit,
                        include_admin=include_admin,
                        aux_terms=aux_terms,
                        doc_type=doc_type,
                        scope=scope,
                    )
                    for hit in part_hits:
                        if hit.node not in combined_hits or hit.score > combined_hits[hit.node].score:
                            combined_hits[hit.node] = hit
                sorted_hits = sorted(
                    combined_hits.values(),
                    key=lambda h: (-h.score, -h.matched_terms, h.node),
                )
                return sorted_hits[:limit]

        # 2. Extract wildcard prefixes (ASCII alnum ending with *, prefix len >= 2)
        wildcards: set[str] = set()
        for m in re.finditer(r"\b([A-Za-z0-9]+)\*", query):
            prefix = m.group(1).casefold()
            if len(prefix) >= 2:
                wildcards.add(prefix)

        # 3. Boolean AND operator or implicit AND
        has_explicit_and = bool(re.search(r"\bAND\b", query))
        if has_explicit_and:
            and_parts = [p.strip() for p in re.split(r"\bAND\b", query) if p.strip()]
            terms: list[str] = []
            for part in and_parts:
                for t in tokenize(part):
                    if t not in terms:
                        terms.append(t)
        else:
            terms = tokenize(query or "")

        phrase_override = "" if (has_explicit_and or wildcards) else None

        hits = self._execute_search(
            query,
            terms=terms,
            aux_terms=aux_terms,
            limit=limit,
            include_admin=include_admin,
            doc_type=doc_type,
            scope=scope,
            raw_phrase_override=phrase_override,
            wildcards=wildcards if wildcards else None,
        )
        if hits:
            return hits

        # Zero-hit fallback for pure ASCII alphanumeric queries (when no aux_terms)
        clean_query = (query or "").strip().casefold()
        is_pure_ascii = clean_query.isascii() and bool(re.match(r"^[a-zA-Z0-9_\-\s*]+$", clean_query))
        if not is_pure_ascii or aux_terms:
            return []

        # 1. Try KB vocabulary typo correction
        from .vocab import suggest_correction
        vocab = self._get_vocab()
        raw_words = re.findall(r"[a-zA-Z0-9]+(?:[-_/.][a-zA-Z0-9]+)*", query or "")
        corrections: dict[str, str] = {}
        for w in raw_words:
            w_fold = w.casefold()
            corr = suggest_correction(w_fold, vocab)
            if corr and corr != w_fold:
                corrections[w_fold] = corr

        if corrections:
            # Reconstruct corrected query for phrase matching
            def _replace_word(m: re.Match[str]) -> str:
                w = m.group(0)
                return corrections.get(w.casefold(), w)

            corrected_query = re.sub(
                r"[a-zA-Z0-9]+(?:[-_/.][a-zA-Z0-9]+)*",
                _replace_word,
                query or "",
            )
            # Separate uncorrected terms from corrected terms (which act as aux_terms with 0.5x weight)
            normal_terms: list[str] = [t for t in terms if t not in corrections]
            corrected_terms: list[str] = []
            for orig, corr in corrections.items():
                for ct in tokenize(corr):
                    if ct not in normal_terms and ct not in corrected_terms:
                        corrected_terms.append(ct)

            fallback_hits = self._execute_search(
                query,
                terms=normal_terms,
                aux_terms=corrected_terms,
                limit=limit,
                include_admin=include_admin,
                doc_type=doc_type,
                scope=scope,
                is_fallback=True,
                query_corrections=corrections,
                raw_phrase_override=corrected_query,
            )
            if fallback_hits:
                return fallback_hits

        # 2. Relaxed AND gate for multi-term queries (coverage >= 50%)
        if len(terms) > 1:
            relaxed_hits = self._execute_search(
                query,
                terms=terms,
                aux_terms=None,
                limit=limit,
                include_admin=include_admin,
                doc_type=doc_type,
                scope=scope,
                is_fallback=True,
                query_corrections=None,
                relaxed_and=True,
            )
            if relaxed_hits:
                return relaxed_hits

        return []

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


def _is_cjk_or_katakana_bigram(term: str) -> bool:
    """Return True if term is a 2-character Katakana or CJK/Hiragana token."""
    if len(term) != 2:
        return False
    for ch in term:
        code = ord(ch)
        is_kata = 0x30A1 <= code <= 0x30FA or code == 0x30FC
        is_cjk = (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0x3040 <= code <= 0x309F
        )
        if not (is_kata or is_cjk):
            return False
    return True


def _score(
    concept: Concept,
    terms: list[str],
    raw_query: str = "",
    aux_terms: list[str] | None = None,
    is_fallback: bool = False,
    query_corrections: dict[str, str] | None = None,
    relaxed_and: bool = False,
    raw_phrase_override: str | None = None,
    wildcards: set[str] | None = None,
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
    phrase_query = (raw_phrase_override if raw_phrase_override is not None else raw_query).strip().casefold()
    phrase_bonus = 0.0
    phrase_matches_list: list[dict[str, Any]] = []
    phrase_mult = 0.5 if (is_fallback and query_corrections) else 1.0
    if len(phrase_query) >= 2:
        if _phrase_matches(phrase_query, title_l):
            pts = 50.0 * phrase_mult
            phrase_bonus += pts
            phrase_matches_list.append({"field": "title", "points": pts})
        if _phrase_matches(phrase_query, desc_l):
            pts = 20.0 * phrase_mult
            phrase_bonus += pts
            phrase_matches_list.append({"field": "description", "points": pts})
        if _phrase_matches(phrase_query, hl_l):
            pts = 15.0 * phrase_mult
            phrase_bonus += pts
            phrase_matches_list.append({"field": "headings_lead", "points": pts})

    phrase_dict: dict[str, Any] = {
        "text": phrase_query,
        "factor": phrase_mult,
        "matches": phrase_matches_list,
        "points": phrase_bonus,
    }

    title_score = 0.0
    tag_score = 0.0
    desc_score = 0.0
    hl_score = 0.0
    body_score = 0.0

    matched_terms = 0
    matched_aux = 0

    breakdown_terms: list[dict[str, Any]] = []
    matched_query_terms: list[str] = []
    missed_query_terms: list[str] = []
    matched_aux_terms: list[str] = []

    for term in terms:
        term_matched = False
        term_matches: list[dict[str, Any]] = []
        is_wildcard = bool(wildcards and term in wildcards)

        if is_wildcard:
            # Wildcard prefix matching (0.7x weight)
            if _field_has_wildcard(title_l, term):
                pts = 20.0 * 0.7
                title_score += pts
                term_matched = True
                term_matches.append({"field": "title", "kind": "wildcard", "points": pts, "factor": 0.7})
            if any(_field_has_wildcard(tag.casefold(), term) for tag in concept.tags):
                pts = 15.0 * 0.7
                tag_score += pts
                term_matched = True
                term_matches.append({"field": "tags", "kind": "wildcard", "points": pts, "factor": 0.7})
            if _field_has_wildcard(desc_l, term):
                pts = 10.0 * 0.7
                desc_score += pts
                term_matched = True
                term_matches.append({"field": "description", "kind": "wildcard", "points": pts, "factor": 0.7})
            if _field_has_wildcard(hl_l, term):
                pts = 5.0 * 0.7
                hl_score += pts
                term_matched = True
                term_matches.append({"field": "headings_lead", "kind": "wildcard", "points": pts, "factor": 0.7})
            cnt = _count_wildcard_in_body(clean_body_l, term)
            if cnt > 0:
                pts = float(cnt) * 0.7
                body_score += pts
                term_matched = True
                term_matches.append({"field": "body", "kind": "wildcard", "points": pts, "factor": 0.7, "count": cnt})
        else:
            term_stem = strip_suffix(term)
            default_kind = "bigram" if _is_cjk_or_katakana_bigram(term) else "exact"
            if term in title_l:
                pts = 20.0
                title_score += pts
                term_matched = True
                term_matches.append({"field": "title", "kind": default_kind, "points": pts})
            if any(term in tag.casefold() for tag in concept.tags):
                pts = 15.0
                tag_score += pts
                term_matched = True
                term_matches.append({"field": "tags", "kind": default_kind, "points": pts})
            if term in desc_l:
                pts = 10.0
                desc_score += pts
                term_matched = True
                term_matches.append({"field": "description", "kind": default_kind, "points": pts})
            if term in hl_l:
                pts = 5.0
                hl_score += pts
                term_matched = True
                term_matches.append({"field": "headings_lead", "kind": default_kind, "points": pts})
            cnt = clean_body_l.count(term)
            if cnt > 0:
                pts = float(cnt)
                body_score += pts
                term_matched = True
                term_matches.append({"field": "body", "kind": default_kind, "points": pts, "count": cnt})

            # Inflectional stem match (0.8x)
            if term_stem:
                if term not in title_l and _field_has_stem(title_l, term_stem):
                    pts = 20.0 * 0.8
                    title_score += pts
                    term_matched = True
                    term_matches.append({"field": "title", "kind": "stem", "points": pts, "factor": 0.8})
                if not any(term in tag.casefold() for tag in concept.tags) and any(
                    _field_has_stem(tag.casefold(), term_stem) for tag in concept.tags
                ):
                    pts = 15.0 * 0.8
                    tag_score += pts
                    term_matched = True
                    term_matches.append({"field": "tags", "kind": "stem", "points": pts, "factor": 0.8})
                if term not in desc_l and _field_has_stem(desc_l, term_stem):
                    pts = 10.0 * 0.8
                    desc_score += pts
                    term_matched = True
                    term_matches.append({"field": "description", "kind": "stem", "points": pts, "factor": 0.8})
                if term not in hl_l and _field_has_stem(hl_l, term_stem):
                    pts = 5.0 * 0.8
                    hl_score += pts
                    term_matched = True
                    term_matches.append({"field": "headings_lead", "kind": "stem", "points": pts, "factor": 0.8})
                if cnt == 0:
                    stem_cnt = _count_stem_in_body(clean_body_l, term_stem)
                    if stem_cnt > 0:
                        pts = float(stem_cnt) * 0.8
                        body_score += pts
                        term_matched = True
                        term_matches.append({"field": "body", "kind": "stem", "points": pts, "factor": 0.8, "count": stem_cnt})

        if term_matched:
            matched_terms += 1
            matched_query_terms.append(term)
        else:
            missed_query_terms.append(term)

        breakdown_terms.append({
            "term": term,
            "role": "query",
            "matches": term_matches,
        })

    if aux_terms:
        corr_orig_map: dict[str, str] = {}
        if query_corrections:
            for orig, corr in query_corrections.items():
                for ct in tokenize(corr):
                    if ct not in corr_orig_map:
                        corr_orig_map[ct] = orig

        for term in aux_terms:
            term_matched = False
            term_matches = []
            is_correction = term in corr_orig_map
            aux_source = "correction" if is_correction else "synonym"
            aux_kind = aux_source

            if term in title_l:
                pts = 10.0
                title_score += pts
                term_matched = True
                term_matches.append({"field": "title", "kind": aux_kind, "points": pts, "factor": 0.5})
            if any(term in tag.casefold() for tag in concept.tags):
                pts = 7.5
                tag_score += pts
                term_matched = True
                term_matches.append({"field": "tags", "kind": aux_kind, "points": pts, "factor": 0.5})
            if term in desc_l:
                pts = 5.0
                desc_score += pts
                term_matched = True
                term_matches.append({"field": "description", "kind": aux_kind, "points": pts, "factor": 0.5})
            if term in hl_l:
                pts = 2.5
                hl_score += pts
                term_matched = True
                term_matches.append({"field": "headings_lead", "kind": aux_kind, "points": pts, "factor": 0.5})
            cnt = clean_body_l.count(term)
            if cnt > 0:
                pts = float(cnt) * 0.5
                body_score += pts
                term_matched = True
                term_matches.append({"field": "body", "kind": aux_kind, "points": pts, "factor": 0.5, "count": cnt})

            if term_matched:
                matched_aux += 1
                matched_aux_terms.append(term)

            aux_entry: dict[str, Any] = {
                "term": term,
                "role": "aux",
                "source": aux_source,
                "matches": term_matches,
            }
            if is_correction:
                aux_entry["corrected_from"] = corr_orig_map[term]
            breakdown_terms.append(aux_entry)

    if matched_terms == 0 and matched_aux == 0:
        return None

    # SimpleWiki AND condition for pure ASCII alphanumeric queries
    is_pure_ascii = clean_query.isascii() and bool(re.match(r"^[a-zA-Z0-9_\-\s]+$", clean_query))
    gate_rule = "none"
    if is_pure_ascii:
        if is_fallback and query_corrections:
            gate_rule = "fallback_corrections"
            total_terms_count = len(terms) + len(aux_terms or [])
            total_matched = matched_terms + matched_aux
            if total_terms_count > 1 and total_matched < total_terms_count:
                return None
        elif relaxed_and:
            gate_rule = "relaxed_and"
            if len(terms) > 1:
                required = (len(terms) + 1) // 2
                if matched_terms < required:
                    return None
        elif not aux_terms and len(terms) > 1:
            gate_rule = "and"
            if matched_terms < len(terms):
                return None

    gate_dict: dict[str, Any] = {
        "rule": gate_rule,
        "matched": matched_query_terms,
        "missed": missed_query_terms,
        "aux_matched": matched_aux_terms,
    }
    if gate_rule == "relaxed_and":
        gate_dict["required"] = (len(terms) + 1) // 2

    fields_dict: dict[str, float] = {
        "title": title_score,
        "tags": tag_score,
        "description": desc_score,
        "headings_lead": hl_score,
        "body": body_score,
    }
    adjustments_list: list[dict[str, Any]] = []

    # Saturated caps (Owner decision: body cap = 10.0)
    hl_capped = min(hl_score, 10.0)
    if hl_score > 10.0:
        adjustments_list.append({
            "kind": "cap",
            "field": "headings_lead",
            "points": hl_capped - hl_score,
        })
    hl_score = hl_capped

    body_capped = min(body_score, 10.0)
    if body_score > 10.0:
        adjustments_list.append({
            "kind": "cap",
            "field": "body",
            "points": body_capped - body_score,
        })
    body_score = body_capped

    score = phrase_bonus + title_score + tag_score + desc_score + hl_score + body_score
    unattenuated_score = score

    # Deprecated attenuation
    tags_lower = {t.lower() for t in concept.tags}
    if (
        any(d in tags_lower for d in ("deprecated", "obsolete", "archived"))
        or concept.meta.get("deprecated") is True
    ):
        score *= 0.3
        adjustments_list.append({
            "kind": "deprecated_attenuation",
            "factor": 0.3,
            "points": score - unattenuated_score,
        })

    breakdown = {
        "terms": breakdown_terms,
        "phrase": phrase_dict,
        "fields": fields_dict,
        "adjustments": adjustments_list,
        "gate": gate_dict,
        "total": score,
    }

    effective_matched = matched_terms + (matched_aux if is_fallback else 0)
    return SearchHit(
        node=concept.rel,
        score=score,
        title=title,
        snippet=_snippet(concept.body, description, all_terms=terms + (aux_terms or [])),
        matched_terms=effective_matched,
        is_fallback=is_fallback,
        query_corrections=query_corrections,
        breakdown=breakdown,
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
