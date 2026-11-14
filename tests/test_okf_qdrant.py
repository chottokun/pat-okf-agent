"""Tests for the dependency-free Qdrant REST adapter (offline fake)."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from okf_kb_agent.wiki.qdrant_backend import QdrantBackend
from okf_kb_agent.wiki.search import BackendUnavailable, get_backend


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


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeQdrant:
    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.requests = []
        self.raw_requests = []

    def __call__(self, request, timeout):
        parsed = urllib.parse.urlparse(request.full_url)
        parts = parsed.path.strip("/").split("/")
        self.requests.append((request.get_method(), parsed.path,
                              request.data))
        self.raw_requests.append(request)
        if len(parts) == 2 and parts[0] == "collections":
            name = urllib.parse.unquote(parts[1])
            if request.get_method() == "GET":
                if name not in self.collections:
                    return FakeResponse({"status": {"error": "not found"}}, 404)
                return FakeResponse({"result": {"config": {"params": {
                    "vectors": {"size": self.collections[name]["size"]}}}}
                })
            if request.get_method() == "PUT":
                body = json.loads(request.data.decode("utf-8"))
                self.collections[name] = {
                    "size": body["vectors"]["size"], "points": {}}
                return FakeResponse({"result": True})
            if request.get_method() == "DELETE":
                existed = self.collections.pop(name, None) is not None
                return FakeResponse({"result": existed}, 200 if existed else 404)
        if len(parts) == 3 and parts[0] == "collections":
            name = urllib.parse.unquote(parts[1])
            collection = self.collections[name]
            if parts[2] == "points":
                body = json.loads(request.data.decode("utf-8"))
                for point in body["points"]:
                    collection["points"]["id:" + str(point["id"])] = point
                return FakeResponse({"result": {"status": "completed"}})
        if len(parts) == 4 and parts[0] == "collections":
            name = urllib.parse.unquote(parts[1])
            collection = self.collections[name]
            if parts[3] == "scroll":
                points = list(collection["points"].values())
                return FakeResponse({"result": {"points": points,
                                                "next_page_offset": None}})
            if parts[3] == "search":
                body = json.loads(request.data.decode("utf-8"))
                query = body["vector"]
                hits = []
                for point in collection["points"].values():
                    vector = point["vector"]
                    dot = sum(a * b for a, b in zip(query, vector))
                    qnorm = math.sqrt(sum(a * a for a in query))
                    vnorm = math.sqrt(sum(b * b for b in vector))
                    score = dot / (qnorm * vnorm) if qnorm and vnorm else 0
                    hits.append({"id": point["id"], "score": score,
                                 "payload": point["payload"]})
                hits.sort(key=lambda item: -item["score"])
                return FakeResponse({"result": hits[:body.get("limit", 10)]})
            if parts[3] == "delete":
                body = json.loads(request.data.decode("utf-8"))
                for point_id in body["points"]:
                    collection["points"].pop("id:" + str(point_id), None)
                return FakeResponse({"result": {"status": "completed"}})
        raise AssertionError(f"unexpected request: {request.get_method()} "
                             f"{parsed.path}")


class QdrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "a.md", "type: Note\ntitle: Amorphous alloy\n",
              "# Amorphous alloy\nMetallic glass.\n")
        write(self.root, "b.md", "type: Note\ntitle: Zeolite\n",
              "# Zeolite\nPorous material.\n")
        self.fake = FakeQdrant()

    def backend(self, **kwargs) -> QdrantBackend:
        return QdrantBackend(
            self.root, embedder=toy_embedder, url="http://fake",
            transport=self.fake, auto_sync=False, **kwargs)

    def test_sync_search_and_incremental_removal(self):
        backend = self.backend()
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 2)
        self.assertEqual(summary["dimensions"], 4)
        hits = backend.search("amorphous")
        self.assertEqual(hits[0].node, "a.md")
        write(self.root, "b.md", "type: Note\ntitle: Zeolite\n",
              "# Zeolite\nZeolite and amorphous composite.\n")
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 1)
        self.assertEqual(summary["unchanged"], 1)
        (self.root / "a.md").unlink()
        summary = backend.sync()
        self.assertEqual(summary["removed"], 1)
        self.assertEqual(backend.stats()["points"], 1)
        self.assertEqual(backend.search("amorphous")[0].node, "b.md")


    def test_batching_keeps_all_points_in_known_map(self):
        backend = self.backend(batch_size=1)
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 2)
        self.assertEqual(backend.stats()["points"], 2)
        self.assertEqual(backend.search("zeolite")[0].node, "b.md")
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 0)
        self.assertEqual(summary["unchanged"], 2)


    def test_empty_wiki_deletes_stale_collection(self):
        backend = self.backend()
        backend.sync()
        self.assertIn(backend.collection, self.fake.collections)
        (self.root / "a.md").unlink()
        (self.root / "b.md").unlink()
        summary = backend.sync()
        self.assertEqual(summary["points"], 0)
        self.assertNotIn(backend.collection, self.fake.collections)
        self.assertEqual(backend.search("anything"), [])

    def test_force_rebuild_and_api_key(self):
        backend = self.backend(api_key="secret")
        backend.sync()
        summary = backend.sync(force=True)
        self.assertEqual(summary["indexed"], 2)
        self.assertTrue(any(
            request.get_header("Api-key") == "secret"
            for request in self.fake.raw_requests))
        self.assertEqual(backend.stats()["collection"],
                         backend.collection)

    def test_backend_factory_and_missing_embedder(self):
        with self.assertRaises(BackendUnavailable):
            QdrantBackend(self.root, auto_sync=False)
        backend = get_backend(
            "qdrant", self.root, embedder=toy_embedder,
            url="http://fake", transport=self.fake, auto_sync=False)
        self.assertIsInstance(backend, QdrantBackend)
        with self.assertRaises(ValueError):
            get_backend("bogus", self.root)


if __name__ == "__main__":
    unittest.main()
