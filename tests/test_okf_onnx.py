"""Tests for the optional Pattern C INT8 embedding seed backend."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.onnx_backend import MicroOnnxBackend
from pat_okf_agent.wiki.search import BackendUnavailable, get_backend


def write(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


def toy_embedder(text: str) -> list[float]:
    text = text.casefold()
    return [
        float(text.count("amorphous")),
        float(text.count("zeolite")),
        float(text.count("专利")),
        0.1,
    ]


class MicroOnnxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "a.md", "type: Note\ntitle: Amorphous alloy\n",
              "# Amorphous alloy\nMetallic glass.\n")
        write(self.root, "b.md", "type: Note\ntitle: Zeolite\n",
              "# Zeolite\nPorous material.\n")
        self.index = self.root / ".okf" / "embeddings"

    def backend(self, **kwargs) -> MicroOnnxBackend:
        return MicroOnnxBackend(
            self.root, embedder=toy_embedder, index_dir=self.index,
            auto_sync=False, **kwargs)

    def test_int8_vectors_are_mmap_searchable(self):
        backend = self.backend()
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 2)
        self.assertEqual(summary["dimensions"], 4)
        self.assertEqual(backend.stats()["bytes"], 8)
        hits = backend.search("amorphous")
        self.assertEqual(hits[0].node, "a.md")
        self.assertGreater(hits[0].score, hits[1].score)
        meta = json.loads((self.index / "meta.json").read_text("utf-8"))
        self.assertEqual(meta["dimensions"], 4)
        self.assertIn("a.md", meta["vectors"])

    def test_incremental_sync_and_dimension_guard(self):
        backend = self.backend()
        backend.sync()
        write(self.root, "b.md", "type: Note\ntitle: Zeolite\n",
              "# Zeolite\nZeolite and amorphous composite.\n")
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 1)
        self.assertEqual(summary["unchanged"], 1)
        self.assertGreater(backend.search("amorphous")[0].score, 0)
        self.assertTrue(backend.search_keywords("zeolite", top_k=1))

    def test_get_backend_onnx_requires_an_embedder(self):
        with self.assertRaises(BackendUnavailable):
            get_backend("onnx", self.root, auto_sync=False)
        backend = get_backend(
            "onnx", self.root, embedder=toy_embedder,
            index_dir=self.index, auto_sync=False)
        self.assertIsInstance(backend, MicroOnnxBackend)


if __name__ == "__main__":
    unittest.main()
