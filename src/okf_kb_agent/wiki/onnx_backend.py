"""Pattern C ONNX path: CPU embeddings with an INT8, mmap-backed seed index.

No ONNX runtime or model is required by the core package.  Supply a callable
``embedder(text) -> list[float]`` (an ONNX Runtime session, Gemma Embedding
adapter, or a deterministic test embedder).  Only seed discovery changes: the
retrieved paths are then handed to the same deterministic Markdown graph.
"""
from __future__ import annotations

import hashlib
import json
import math
import mmap
import os
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .graph import build_graph
from .model import extract_sections, iter_concepts, load_concept, tokenize
from .search import BackendUnavailable, SearchHit

__all__ = ["MicroOnnxBackend", "OnnxEmbedder"]

Embedder = Callable[[str], list[float]]


class OnnxEmbedder:
    """Thin optional ONNX Runtime wrapper; tokenization is caller-supplied."""

    def __init__(self, model_path: str | Path, tokenizer: Callable,
                 *, input_name: str | None = None):
        try:
            import onnxruntime  # type: ignore
        except ImportError as exc:
            raise BackendUnavailable(
                "onnxruntime is not installed; `uv add onnxruntime` and "
                "provide a tokenizer, or pass a custom embedder") from exc
        self.session = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"])
        self.tokenizer = tokenizer
        self.input_name = input_name or self.session.get_inputs()[0].name

    def __call__(self, text: str) -> list[float]:
        feeds = self.tokenizer(text)
        outputs = self.session.run(None, {self.input_name: feeds})
        values = outputs[0]
        # Flatten [1, dim] / [dim].
        if isinstance(values, (list, tuple)) and values and isinstance(
                values[0], (list, tuple)):
            values = values[0]
        return [float(value) for value in values]


@dataclass(frozen=True)
class _Vector:
    offset: int
    dimensions: int
    norm: float
    sha1: str
    title: str
    description: str


class MicroOnnxBackend:
    """INT8 embedding seed index; Markdown links remain the graph of record."""

    def __init__(self, root: str | Path, *, embedder: Embedder | None = None,
                 index_dir: str | Path | None = None,
                 auto_sync: bool = True):
        self.root = Path(root)
        self.index_dir = (Path(index_dir) if index_dir is not None
                          else self.root / ".okf" / "embeddings")
        self.meta_path = self.index_dir / "meta.json"
        self.vector_path = self.index_dir / "vectors.bin"
        if embedder is None:
            raise BackendUnavailable(
                "Pattern C needs an embedder. Pass `embedder=` (ONNX Runtime, "
                "Gemma Embedding adapter, or a test callable).")
        self.embedder = embedder
        self.dimensions: int | None = None
        self.vectors: dict[str, _Vector] = {}
        self._load_meta()
        if auto_sync:
            self.sync()

    # -- index -------------------------------------------------------------
    def _load_meta(self) -> None:
        if not self.meta_path.exists():
            return
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.dimensions = int(data.get("dimensions") or 0) or None
        for path, raw in (data.get("vectors") or {}).items():
            self.vectors[path] = _Vector(
                offset=int(raw["offset"]), dimensions=int(raw["dimensions"]),
                norm=float(raw["norm"]), sha1=str(raw["sha1"]),
                title=str(raw.get("title") or ""),
                description=str(raw.get("description") or ""))

    def _write_meta(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "dimensions": self.dimensions,
            "vectors": {
                path: {
                    "offset": vector.offset,
                    "dimensions": vector.dimensions,
                    "norm": vector.norm,
                    "sha1": vector.sha1,
                    "title": vector.title,
                    "description": vector.description,
                }
                for path, vector in sorted(self.vectors.items())
            },
        }
        tmp = self.meta_path.with_name(self.meta_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.meta_path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def sync(self, *, force: bool = False) -> dict[str, int]:
        concepts = {concept.rel: concept
                    for concept in iter_concepts(self.root)}
        changed = 0
        unchanged = 0
        removed = 0
        # If the first embedding changes dimension, rebuild all vectors.
        rebuild = force
        if not rebuild and concepts:
            first = next(iter(concepts.values()))
            probe = self._embed(first.title + "\n" + first.body)
            if self.dimensions is not None and len(probe) != self.dimensions:
                rebuild = True
        if rebuild:
            self.vectors.clear()
            self.dimensions = None
            if self.vector_path.exists():
                self.vector_path.unlink()

        self.index_dir.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if self.vector_path.exists() else "w+b"
        with open(self.vector_path, mode) as handle:
            if rebuild:
                handle.truncate(0)
            handle.seek(0, os.SEEK_END)
            for rel, concept in concepts.items():
                digest = _sha1((self.root / rel).read_bytes())
                existing = self.vectors.get(rel)
                if (not force and existing and existing.sha1 == digest
                        and (self.dimensions is None
                             or existing.dimensions == self.dimensions)):
                    unchanged += 1
                    continue
                values = self._embed(concept.title + "\n" + concept.body)
                if not values:
                    continue
                if self.dimensions is None:
                    self.dimensions = len(values)
                if len(values) != self.dimensions:
                    raise ValueError(
                        f"embedding dimension mismatch for {rel}: "
                        f"{len(values)} != {self.dimensions}")
                quantized, norm = _quantize(values)
                offset = handle.tell()
                handle.write(quantized.tobytes())
                self.vectors[rel] = _Vector(
                    offset=offset, dimensions=len(values), norm=norm,
                    sha1=digest, title=concept.title,
                    description=concept.description)
                changed += 1
            handle.flush()

        for rel in set(self.vectors) - set(concepts):
            del self.vectors[rel]
            removed += 1
        self._write_meta()
        return {"indexed": changed, "unchanged": unchanged,
                "removed": removed, "dimensions": self.dimensions or 0,
                "path": str(self.index_dir)}

    def _embed(self, text: str) -> list[float]:
        values = self.embedder(text)
        return [float(value) for value in values]

    # -- search ------------------------------------------------------------
    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        if limit <= 0 or not self.vectors or not self.vector_path.exists():
            return []
        query_vector = self._embed(query or "")
        if not query_vector:
            return []
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        if query_norm == 0:
            return []
        hits: list[SearchHit] = []
        with open(self.vector_path, "rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for rel, vector in self.vectors.items():
                    if vector.dimensions != len(query_vector):
                        continue
                    raw = mm[vector.offset:vector.offset + vector.dimensions]
                    values = array("b")
                    values.frombytes(raw)
                    dot = sum(q * v for q, v in zip(query_vector, values))
                    score = dot / (query_norm * vector.norm) if vector.norm else 0
                    hits.append(SearchHit(
                        node=rel, score=score, title=vector.title or rel,
                        snippet=vector.description,
                        matched_terms=sum(
                            1 for term in set(tokenize(query))
                            if term in (vector.title + " " +
                                        vector.description).casefold()) or 1))
        hits.sort(key=lambda hit: (-hit.score, hit.node))
        return hits[:limit]

    def search_keywords(self, query: str, top_k: int = 3) -> list[str]:
        return [hit.node for hit in self.search(query, limit=top_k)]

    # -- graph driver interface -------------------------------------------
    def get_outgoing_edges(self, source_path: str) -> list[dict]:
        graph = build_graph(self.root)
        return [edge.as_dict() for edge in
                graph.edge_objects.get(source_path, [])]

    def read_section(self, path: str, scope: str) -> str:
        concept = load_concept(self.root / path, self.root)
        wanted = scope.lstrip("# ").strip().casefold()
        for section in extract_sections(concept.body):
            if (section.scope and section.scope.casefold() == wanted) or (
                    section.heading.casefold() == wanted):
                return section.text
        if concept.scope.casefold() == wanted:
            return concept.body
        return ""

    def stats(self) -> dict:
        return {"backend": "onnx", "index": str(self.index_dir),
                "vectors": len(self.vectors),
                "dimensions": self.dimensions or 0,
                "bytes": (self.vector_path.stat().st_size
                          if self.vector_path.exists() else 0)}


def _quantize(values: list[float]) -> tuple[array, float]:
    maximum = max((abs(value) for value in values), default=0.0)
    scale = maximum / 127.0 if maximum else 1.0
    quantized = array("b", (
        max(-127, min(127, round(value / scale))) for value in values))
    norm = math.sqrt(sum(value * value for value in quantized))
    return quantized, norm


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()
