"""Qdrant REST adapter for Pattern C seed search.

Qdrant is optional infrastructure.  This adapter uses the HTTP API directly so
the core package remains dependency-free, and accepts an injectable transport
for offline tests.  Markdown remains authoritative: Qdrant stores only derived
vectors and payload pointers.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .model import iter_concepts, tokenize
from .search import BackendUnavailable, SearchHit

__all__ = ["QdrantBackend", "QdrantError"]

Transport = Callable[[urllib.request.Request, float], Any]


class QdrantError(BackendUnavailable):
    """A Qdrant API failure, optionally with an HTTP status."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class QdrantBackend:
    """Sync Markdown concepts into Qdrant and use it for seed search."""

    def __init__(self, root: str | Path, *, embedder: Callable | None = None,
                 url: str = "http://localhost:6333",
                 collection: str | None = None,
                 api_key: str | None = None, timeout: float = 60.0,
                 transport: Transport | None = None,
                 auto_sync: bool = True, batch_size: int = 64):
        if embedder is None:
            raise BackendUnavailable(
                "Pattern C/Qdrant needs an embedder. Pass `embedder=` "
                "(OnnxEmbedder, HttpEmbedder/GemmaEmbedder, or a callable).")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.root = Path(root)
        self.embedder = embedder
        self.url = url.rstrip("/")
        digest = hashlib.sha1(str(self.root.resolve()).encode("utf-8")).hexdigest()
        self.collection = collection or f"okf_{digest[:12]}"
        self.api_key = api_key
        self.timeout = float(timeout)
        self.transport = transport or _default_transport
        self.batch_size = batch_size
        self.dimensions: int | None = None
        self._known: dict[str, str] = {}
        if auto_sync:
            self.sync()

    # -- lifecycle ---------------------------------------------------------
    def sync(self, *, force: bool = False) -> dict[str, Any]:
        concepts = {concept.rel: concept
                    for concept in iter_concepts(self.root)}
        if not concepts:
            # Do not leave points from a previously non-empty wiki searchable.
            self._delete_collection()
            self._known.clear()
            self.dimensions = None
            return {"indexed": 0, "unchanged": 0, "removed": 0,
                    "dimensions": 0, "collection": self.collection,
                    "points": 0}
        if force:
            self._delete_collection()
            self._known.clear()
            self.dimensions = None
        probe_rel, first = next(iter(concepts.items()))
        probe = self._embed(first.title + "\n" + first.body)
        if not probe:
            raise BackendUnavailable("embedder returned an empty vector")
        if self.dimensions is None:
            self.dimensions = len(probe)
        self._ensure_collection(len(probe))
        existing = self._scroll()
        self._known = {node: payload.get("sha1", "")
                       for node, payload in existing.items()}
        pending: list[dict[str, Any]] = []
        upserted: list[dict[str, Any]] = []
        changed = unchanged = 0
        for rel, concept in concepts.items():
            digest = _sha1((self.root / rel).read_bytes())
            if not force and self._known.get(rel) == digest:
                unchanged += 1
                continue
            values = (probe if rel == probe_rel
                      else self._embed(concept.title + "\n" + concept.body))
            if not values:
                raise BackendUnavailable(
                    f"embedder returned an empty vector for {rel}")
            if len(values) != self.dimensions:
                raise ValueError(
                    f"embedding dimension mismatch for {rel}: "
                    f"{len(values)} != {self.dimensions}")
            pending.append({
                "id": _point_id(rel),
                "vector": values,
                "payload": {
                    "node": rel,
                    "sha1": digest,
                    "title": concept.title,
                    "description": concept.description,
                },
            })
            changed += 1
            if len(pending) >= self.batch_size:
                self._upsert(pending)
                upserted.extend(pending)
                pending = []
        if pending:
            self._upsert(pending)
            upserted.extend(pending)

        removed_nodes = sorted(set(self._known) - set(concepts))
        if removed_nodes:
            self._delete([_point_id(node) for node in removed_nodes])
            for node in removed_nodes:
                self._known.pop(node, None)
        for point in upserted:
            self._known[point["payload"]["node"]] = point["payload"]["sha1"]
        return {"indexed": changed, "unchanged": unchanged,
                "removed": len(removed_nodes),
                "dimensions": self.dimensions or 0,
                "collection": self.collection, "points": len(self._known)}

    # -- search ------------------------------------------------------------
    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        if limit <= 0 or not query.strip():
            return []
        if self.dimensions is None:
            return []
        values = self._embed(query)
        if not values:
            raise BackendUnavailable("embedder returned an empty query vector")
        if len(values) != self.dimensions:
            raise BackendUnavailable(
                f"query embedding dimension {len(values)} != collection "
                f"dimension {self.dimensions}")
        result = self._request("POST", "/points/search", {
            "vector": values, "limit": max(1, limit),
            "with_payload": True,
        })
        hits: list[SearchHit] = []
        for item in result or []:
            payload = item.get("payload") or {}
            node = payload.get("node")
            if not isinstance(node, str):
                continue
            title = str(payload.get("title") or node)
            description = str(payload.get("description") or "")
            matched = sum(1 for term in set(tokenize(query))
                          if term in (title + " " + description).casefold())
            hits.append(SearchHit(
                node=node, score=float(item.get("score") or 0.0),
                title=title, snippet=description, matched_terms=matched or 1))
        hits.sort(key=lambda hit: (-hit.score, hit.node))
        return hits[:limit]

    def stats(self) -> dict[str, Any]:
        return {"backend": "qdrant", "url": self.url,
                "collection": self.collection,
                "points": len(self._known),
                "dimensions": self.dimensions or 0}

    # -- API ---------------------------------------------------------------
    def _ensure_collection(self, dimensions: int) -> None:
        status, data = self._raw("GET", "")
        if status == 404:
            self._request("PUT", "", {
                "vectors": {"size": dimensions, "distance": "Cosine"},
            })
            return
        if status >= 400:
            raise QdrantError(f"Qdrant collection probe failed: HTTP {status}",
                              status=status)
        vectors = (((data.get("result") or {}).get("config") or {})
                   .get("params") or {}).get("vectors") or {}
        size = vectors.get("size") if isinstance(vectors, dict) else None
        if isinstance(size, int) and size != dimensions:
            raise ValueError(
                f"Qdrant collection dimension {size} != embedder {dimensions}")

    def _scroll(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        offset = None
        while True:
            body: dict[str, Any] = {"limit": 256, "with_payload": True,
                                    "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            result = self._request("POST", "/points/scroll", body) or {}
            for point in result.get("points") or []:
                payload = point.get("payload") or {}
                node = payload.get("node")
                if isinstance(node, str):
                    found[node] = payload
            offset = result.get("next_page_offset")
            if not offset:
                break
        return found

    def _upsert(self, points: list[dict[str, Any]]) -> None:
        self._request("PUT", "/points?wait=true", {"points": points})

    def _delete(self, ids: list[int]) -> None:
        self._request("POST", "/points/delete?wait=true", {"points": ids})

    def _delete_collection(self) -> None:
        status, _ = self._raw("DELETE", "")
        if status not in (200, 202, 204, 404):
            raise QdrantError(
                f"Qdrant collection delete failed: HTTP {status}", status=status)

    def _request(self, method: str, suffix: str,
                 payload: dict[str, Any] | None = None) -> Any:
        status, data = self._raw(method, suffix, payload)
        if status >= 400:
            raise QdrantError(
                f"Qdrant {method} {suffix or '/'} failed: HTTP {status}",
                status=status)
        return data.get("result", data)

    def _raw(self, method: str, suffix: str,
             payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = f"{self.url}/collections/{urllib.parse.quote(self.collection, safe='')}{suffix}"
        body = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None else None)
        headers = {"Content-Type": "application/json",
                   "User-Agent": "okf-qdrant/0.1"}
        if self.api_key:
            headers["api-key"] = self.api_key
        request = urllib.request.Request(
            url, data=body, headers=headers, method=method)
        try:
            response = self.transport(request, self.timeout)
            try:
                raw = response.read() if hasattr(response, "read") else response
                status = int(getattr(response, "status",
                                     getattr(response, "code", 200)) or 200)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except Exception as exc:  # noqa: BLE001 - optional adapter boundary
            close = getattr(exc, "close", None)
            try:
                status = getattr(exc, "code", None)
                if status is None:
                    raise BackendUnavailable(
                        f"Qdrant request failed: "
                        f"{type(exc).__name__}: {exc}") from exc
                raw = getattr(exc, "body", b"") or b""
            finally:
                if callable(close):
                    close()
        if isinstance(raw, (dict, list)):
            data = raw
        else:
            try:
                data = json.loads(raw.decode("utf-8")
                                  if isinstance(raw, bytes) else str(raw or "{}"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = {}
        return status, data

    def _embed(self, text: str) -> list[float]:
        return [float(value) for value in self.embedder(text)]


def _point_id(node: str) -> int:
    return int(hashlib.sha1(node.encode("utf-8")).hexdigest()[:16], 16) \
        & 0x7fffffffffffffff


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _default_transport(request: urllib.request.Request,
                       timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)
