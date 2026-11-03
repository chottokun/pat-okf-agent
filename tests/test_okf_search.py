"""Tests for the local (Pattern A) search backend and adapter boundary."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pat_okf_agent.wiki.search import (
    BackendUnavailable,
    LocalSearchBackend,
    pattern_for,
    get_backend,
)


def write(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


class LocalSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "a.md",
              "type: Note\ntitle: Amorphous alloy\ntags: [metal]\n",
              "A bulk metallic glass composition.\n")
        write(self.root, "b.md",
              "type: Note\ntitle: Coating\n",
              "An amorphous film used for coating.\n")
        write(self.root, "c.md", "type: Note\ntitle: Unrelated\n",
              "Nothing to see.\n")
        self.backend = LocalSearchBackend(self.root)

    def test_title_match_outranks_body_match(self):
        hits = self.backend.search("amorphous")
        self.assertEqual([h.node for h in hits], ["a.md", "b.md"])
        self.assertGreater(hits[0].score, hits[1].score)

    def test_multi_term_coverage_is_ranked_higher(self):
        hits = self.backend.search("amorphous coating")
        self.assertEqual(hits[0].node, "b.md")
        self.assertEqual(hits[0].matched_terms, 2)

    def test_limit_and_empty_query(self):
        self.assertEqual(len(self.backend.search("amorphous", limit=1)), 1)
        self.assertEqual(self.backend.search(""), [])
        self.assertEqual(self.backend.search("   "), [])

    def test_no_match_returns_empty(self):
        self.assertEqual(self.backend.search("zzzznotpresent"), [])

    def test_snippet_contains_query_context(self):
        hit = self.backend.search("film")[0]
        self.assertEqual(hit.node, "b.md")
        self.assertIn("film", hit.snippet.lower())

    def test_ripgrep_candidates_are_used_with_fallback(self):
        with patch("pat_okf_agent.wiki.search.shutil.which",
                   return_value="/usr/bin/rg"), patch(
                       "pat_okf_agent.wiki.search.subprocess.run",
                       return_value=subprocess.CompletedProcess(
                           [], 0, stdout="./a.md\n", stderr="")) as run:
            hits = LocalSearchBackend(
                self.root, use_ripgrep=True).search("amorphous")
        self.assertEqual([hit.node for hit in hits], ["a.md"])
        self.assertIn("--fixed-strings", run.call_args.args[0])

    def test_pattern_thresholds(self):
        self.assertEqual(pattern_for(0), "A")
        self.assertEqual(pattern_for(499), "A")
        self.assertEqual(pattern_for(500), "A")
        self.assertEqual(pattern_for(501), "B")
        self.assertEqual(pattern_for(2000), "B")
        self.assertEqual(pattern_for(2001), "C")

    def test_backend_factory_is_reversible(self):
        backend = get_backend("local", self.root)
        self.assertIsInstance(backend, LocalSearchBackend)
        self.assertIsInstance(
            get_backend("local", self.root, db_path="/tmp/ignored.db"),
            LocalSearchBackend)
        with self.assertRaises(BackendUnavailable):
            get_backend("qdrant", self.root)
        with self.assertRaises(ValueError):
            get_backend("nope", self.root)


if __name__ == "__main__":
    unittest.main()
