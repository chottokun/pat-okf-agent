"""Pattern B: SQLite FTS5 search plus an adjacency-list edge table.

The Markdown files remain the source of truth.  ``SQLiteSearchBackend`` syncs
changed files incrementally into a single derived SQLite database, so the wiki
can be copied, versioned and rebuilt without a server process.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .model import (
    extract_sections,
    iter_concepts,
    load_concept,
    scope_from_heading,
    scope_of,
)
from .search import BackendUnavailable, SearchHit

__all__ = ["SQLiteSearchBackend", "fts5_available"]

_TERM_RE = re.compile(r"[0-9A-Za-z_\u0080-\u00ff\u0370-\u03ff\u3040-\u30ff\u4e00-\u9fff]+")


def fts5_available() -> bool:
    """Return True when the linked SQLite build includes FTS5."""
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True


class SQLiteSearchBackend:
    """Deterministic FTS5 + edge-table backend (Pattern B)."""

    def __init__(self, root: str | Path, *,
                 db_path: str | Path | None = None,
                 auto_sync: bool = True):
        self.root = Path(root)
        self.db_path = (Path(db_path) if db_path is not None
                        else self.root / ".okf" / "index.sqlite3")
        if not fts5_available():
            raise BackendUnavailable(
                "SQLite FTS5 is not available in this Python build")
        self._ensure_schema()
        if auto_sync:
            self.sync()

    # -- schema / sync -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        schema = """
                CREATE VIRTUAL TABLE IF NOT EXISTS okf_fts USING fts5(
                    path UNINDEXED,
                    type,
                    title,
                    description,
                    tags,
                    body,
                    tokenize = 'trigram'
                );
                CREATE TABLE IF NOT EXISTS okf_edges(
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    heading_scope TEXT NOT NULL,
                    anchor_text TEXT NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS okf_edges_source
                    ON okf_edges(source_path);
                CREATE INDEX IF NOT EXISTS okf_edges_target
                    ON okf_edges(target_path);
                CREATE TABLE IF NOT EXISTS okf_files(
                    path TEXT PRIMARY KEY,
                    sha1 TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL
                );
                """
        try:
            with self._connection() as connection:
                connection.executescript(schema)
        except sqlite3.OperationalError:
            # Older SQLite builds may lack the trigram tokenizer.
            fallback = schema.replace("tokenize = 'trigram'",
                                      "tokenize = 'unicode61'")
            with self._connection() as connection:
                connection.executescript(fallback)

    def sync(self, *, force: bool = False) -> dict[str, int]:
        """Incrementally synchronise changed Markdown files into SQLite."""
        concepts = {concept.rel: concept
                    for concept in iter_concepts(self.root)}
        with self._connection() as connection:
            existing = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT path, sha1 FROM okf_files")
            }
            indexed = removed = unchanged = 0
            for rel, concept in concepts.items():
                path = concept.path
                stat = path.stat()
                digest = _sha1(path.read_bytes())
                if not force and existing.get(rel) == digest:
                    unchanged += 1
                    continue
                self._delete_document(connection, rel)
                connection.execute(
                    "INSERT INTO okf_fts"
                    "(path, type, title, description, tags, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rel, concept.type, concept.title,
                     concept.description, ", ".join(concept.tags),
                     concept.body),
                )
                for link in concept.links:
                    if link.external or link.resolved is None:
                        continue
                    target = concepts.get(link.resolved)
                    scope = (link.section_scope
                             or (scope_of(target.meta) if target else
                                 "taxonomy"))
                    connection.execute(
                        "INSERT INTO okf_edges"
                        "(source_path, target_path, heading_scope, "
                        "anchor_text, heading, context) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (rel, link.resolved, scope, link.text,
                         link.heading, link.context),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO okf_files"
                    "(path, sha1, mtime_ns, size) VALUES (?, ?, ?, ?)",
                    (rel, digest, stat.st_mtime_ns, stat.st_size),
                )
                indexed += 1

            for rel in set(existing) - set(concepts):
                self._delete_document(connection, rel)
                connection.execute(
                    "DELETE FROM okf_files WHERE path = ?", (rel,))
                removed += 1
            connection.commit()

        return {"indexed": indexed, "removed": removed,
                "unchanged": unchanged, "path": str(self.db_path)}

    @staticmethod
    def _delete_document(connection: sqlite3.Connection, rel: str) -> None:
        connection.execute("DELETE FROM okf_fts WHERE path = ?", (rel,))
        connection.execute(
            "DELETE FROM okf_edges WHERE source_path = ?", (rel,))

    # -- search ------------------------------------------------------------
    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        terms = _TERM_RE.findall(query or "")
        if not terms or limit <= 0:
            return []
        expression = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT path, title, description, "
                "snippet(okf_fts, 5, '', '', '…', 20), "
                "bm25(okf_fts, 0.0, 1.0, 10.0, 4.0, 3.0, 1.0) AS rank "
                "FROM okf_fts WHERE okf_fts MATCH ? "
                "ORDER BY rank, path LIMIT ?",
                (expression, limit),
            ).fetchall()
        hits: list[SearchHit] = []
        for path, title, description, snippet, rank in rows:
            text = (title + " " + (snippet or "") + " " +
                    (description or "")).casefold()
            matched = sum(1 for term in terms if term.casefold() in text)
            # bm25 is lower-is-better (usually negative); expose higher-is-better.
            score = -float(rank) if rank is not None else 0.0
            hits.append(SearchHit(
                node=path, score=score, title=title or path,
                snippet=snippet or description or "", matched_terms=matched))
        if not hits and any(len(term) < 3 for term in terms):
            # Trigram indexes cannot match 1-2 character terms.
            from .search import LocalSearchBackend
            return LocalSearchBackend(
                self.root, use_ripgrep=False).search(query, limit=limit)
        return hits

    # -- graph driver interface -------------------------------------------
    def get_outgoing_edges(self, source_path: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT source_path, target_path, heading_scope, "
                "anchor_text, heading, context FROM okf_edges "
                "WHERE source_path = ? ORDER BY target_path, anchor_text",
                (source_path,),
            ).fetchall()
        keys = ("source_path", "target_path", "heading_scope",
                "anchor_text", "heading", "context")
        return [dict(zip(keys, row)) for row in rows]

    def get_incoming_edges(self, target_path: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT source_path, target_path, heading_scope, "
                "anchor_text, heading, context FROM okf_edges "
                "WHERE target_path = ? ORDER BY source_path, anchor_text",
                (target_path,),
            ).fetchall()
        keys = ("source_path", "target_path", "heading_scope",
                "anchor_text", "heading", "context")
        return [dict(zip(keys, row)) for row in rows]

    def search_keywords(self, query: str, top_k: int = 3) -> list[str]:
        return [hit.node for hit in self.search(query, limit=top_k)]

    def read_section(self, path: str, scope: str) -> str:
        concept = load_concept(self.root / path, self.root)
        raw = scope.strip()
        wanted = (scope_from_heading(raw.lstrip("# ").strip())
                  or raw.lstrip("# ").strip().casefold())
        for section in extract_sections(concept.body):
            if (section.scope and section.scope.casefold() == wanted) or (
                    section.heading.casefold() ==
                    raw.lstrip("# ").strip().casefold()):
                return section.text
        if concept.scope.casefold() == wanted:
            return concept.body
        return ""

    def stats(self) -> dict:
        with self._connection() as connection:
            documents = connection.execute(
                "SELECT COUNT(*) FROM okf_fts").fetchone()[0]
            edges = connection.execute(
                "SELECT COUNT(*) FROM okf_edges").fetchone()[0]
            files = connection.execute(
                "SELECT COUNT(*) FROM okf_files").fetchone()[0]
        return {"backend": "sqlite", "db": str(self.db_path),
                "documents": documents, "edges": edges, "files": files}

    def close(self) -> None:
        # Connections are short-lived; retained for adapter symmetry.
        return None


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()
