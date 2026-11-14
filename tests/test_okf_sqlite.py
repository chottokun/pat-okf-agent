"""Tests for the Pattern B SQLite-FTS5 backend."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from okf_kb_agent.wiki.context import ContextResolver
from okf_kb_agent.wiki.search import (
    LocalSearchBackend,
    get_backend,
)
from okf_kb_agent.wiki.sqlite_backend import (
    SQLiteSearchBackend,
    fts5_available,
)

FTS5 = fts5_available()


def write(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


@unittest.skipUnless(FTS5, "SQLite FTS5 is unavailable")
class SQLiteBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(
            self.root, "seeds/amorphous.md", "type: Note\ntitle: Amorphous\n",
            "# Amorphous\n\n## Taxonomy\n"
            "[Background](background.md)\n\n"
            "## Gotchas / Anti-Pitfalls\n"
            "[failure](../gotchas/failure.md)\n",
        )
        write(self.root, "seeds/background.md", "type: Note\ntitle: Background\n",
              "# Background\n\nZeolite support.\n")
        write(self.root, "gotchas/failure.md", "type: Gotcha\ntitle: Failure\n",
              "# Failure\n\nAmorphous brittleness.\n")
        self.db = self.root / ".okf" / "test.sqlite3"

    def backend(self) -> SQLiteSearchBackend:
        return SQLiteSearchBackend(self.root, db_path=self.db, auto_sync=False)

    def test_sync_search_and_edges_keep_heading_scope(self):
        backend = self.backend()
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 3)
        hits = backend.search("amorphous", limit=10)
        self.assertEqual(hits[0].node, "seeds/amorphous.md")
        self.assertEqual(backend.stats()["documents"], 3)
        edges = backend.get_outgoing_edges("seeds/amorphous.md")
        self.assertEqual({edge["target_path"] for edge in edges},
                         {"seeds/background.md", "gotchas/failure.md"})
        by_target = {edge["target_path"]: edge for edge in edges}
        self.assertEqual(by_target["seeds/background.md"]["heading_scope"],
                         "taxonomy")
        self.assertEqual(by_target["gotchas/failure.md"]["heading_scope"],
                         "gotchas")
        self.assertEqual(by_target["seeds/background.md"]["anchor_text"],
                         "Background")
        self.assertEqual(backend.search_keywords("amorphous", top_k=1),
                         ["seeds/amorphous.md"])
        self.assertIn("Background",
                      backend.read_section("seeds/amorphous.md", "taxonomy"))

    def test_incremental_sync_updates_and_removes(self):
        backend = self.backend()
        backend.sync()
        write(self.root, "seeds/background.md", "type: Note\ntitle: Background\n",
              "# Background\n\nZeolite and lithium support.\n")
        summary = backend.sync()
        self.assertEqual(summary["indexed"], 1)
        self.assertEqual(summary["unchanged"], 2)
        self.assertTrue(backend.search("lithium"))

        (self.root / "gotchas/failure.md").unlink()
        summary = backend.sync()
        self.assertEqual(summary["removed"], 1)
        self.assertEqual(backend.get_outgoing_edges(
            "seeds/amorphous.md")[1]["target_path"], "seeds/background.md")
        self.assertTrue(all(hit.node != "gotchas/failure.md"
                            for hit in backend.search("brittleness")))

    def test_auto_backend_follows_node_count_trigger(self):
        local = get_backend("auto", self.root)
        self.assertIsInstance(local, LocalSearchBackend)
        sqlite = get_backend("auto", self.root, node_count=501)
        self.assertIsInstance(sqlite, SQLiteSearchBackend)
        self.assertEqual(sqlite.stats()["documents"], 3)

    def test_context_resolver_can_use_sqlite_seeds(self):
        backend = self.backend()
        backend.sync()
        bundle = ContextResolver(self.root, backend=backend).resolve(
            "amorphous", max_nodes=3)
        self.assertIn("seeds/amorphous.md",
                      [node.node for node in bundle.nodes])

    def test_backend_interface_is_reversible(self):
        backend = self.backend()
        backend.sync()
        hits = backend.search("amorphous")
        self.assertTrue(hits)
        self.assertTrue(all(hasattr(hit, "node") for hit in hits))
        self.assertIn("documents", backend.stats())

    def test_term_re_supports_greek_and_latin1_supplement(self):
        from okf_kb_agent.wiki.sqlite_backend import _TERM_RE
        self.assertEqual(_TERM_RE.findall("α-olefin"), ["α", "olefin"])
        self.assertEqual(_TERM_RE.findall("Böhm"), ["Böhm"])

    def test_search_greek_and_latin1_terms(self):
        backend = self.backend()
        write(
            self.root, "seeds/catalyst.md", "type: Note\ntitle: Böhm Catalyst\n",
            "# Böhm Catalyst\n\nPolymerization of α-olefin compounds.\n",
        )
        backend.sync()
        hits_boehm = backend.search("Böhm")
        self.assertTrue(hits_boehm)
        self.assertEqual(hits_boehm[0].node, "seeds/catalyst.md")
        hits_alpha = backend.search("α-olefin")
        self.assertTrue(hits_alpha)
        self.assertEqual(hits_alpha[0].node, "seeds/catalyst.md")


if __name__ == "__main__":
    unittest.main()
