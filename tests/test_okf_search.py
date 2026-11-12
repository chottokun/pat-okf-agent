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

    def test_body_saturation_cap_prevents_body_heavy_outranking_title(self):
        write(self.root, "patent.md", "type: Patent\ntitle: Unrelated Device\n",
              "Introductory unrelated background.\n\n" + "vapor chamber " * 100 + "\n")
        write(self.root, "concept.md", "type: Concept\ntitle: Vapor Chamber\n",
              "A thermal ground plane.\n")
        hits = self.backend.search("vapor chamber")
        self.assertEqual(hits[0].node, "concept.md")
        patent_hit = next(h for h in hits if h.node == "patent.md")
        self.assertLessEqual(patent_hit.score, 10.0)

    def test_admin_files_excluded_by_default(self):
        write(self.root, "BUILDLOG.md", "title: Build Log\n",
              "amorphous metallic glass amorphous\n")
        write(self.root, "SUMMARY.md", "title: Summary\n",
              "amorphous metallic glass amorphous\n")
        hits = self.backend.search("amorphous")
        nodes = [h.node for h in hits]
        self.assertNotIn("BUILDLOG.md", nodes)
        self.assertNotIn("SUMMARY.md", nodes)

        hits_admin = self.backend.search("amorphous", include_admin=True)
        admin_nodes = [h.node for h in hits_admin]
        self.assertIn("BUILDLOG.md", admin_nodes)
        self.assertIn("SUMMARY.md", admin_nodes)

    def test_headings_and_lead_scoring(self):
        write(self.root, "hl.md", "type: Concept\ntitle: Thermal Device\n",
              "## Vapor Chamber\nThis lead paragraph describes cooling.\n")
        write(self.root, "body_only.md", "type: Concept\ntitle: Thermal Device 2\n",
              "Introduction text here.\n\nLater in body vapor chamber appears.\n")
        hits = self.backend.search("vapor chamber")
        score_hl = next(h.score for h in hits if h.node == "hl.md")
        score_body = next(h.score for h in hits if h.node == "body_only.md")
        self.assertGreater(score_hl, score_body)

    def test_phrase_bonus(self):
        write(self.root, "phrase.md", "type: Note\ntitle: Heat Spreader Board\n",
              "Device details.\n")
        write(self.root, "split.md", "type: Note\ntitle: Spreader of Excess Heat\n",
              "Device details.\n")
        hits = self.backend.search("heat spreader")
        self.assertEqual(hits[0].node, "phrase.md")
        self.assertGreater(hits[0].score, hits[1].score + 40.0)

    def test_deprecated_attenuation(self):
        write(self.root, "active.md", "type: Note\ntitle: Dielectric Liquid\n",
              "Coolant body.\n")
        write(self.root, "dep.md",
              "type: Note\ntitle: Dielectric Liquid\ntags: [deprecated]\n",
              "Coolant body.\n")
        hits = self.backend.search("Dielectric Liquid")
        active_score = next(h.score for h in hits if h.node == "active.md")
        dep_score = next(h.score for h in hits if h.node == "dep.md")
        self.assertAlmostEqual(dep_score, active_score * 0.3, places=2)

    def test_inline_synonym_auxiliary_terms(self):
        write(self.root, "syn_target.md", "type: Note\ntitle: Vapor Chamber\n",
              "Device.\n")
        # Query is "均熱板", with auxiliary term "vapor chamber"
        hits = self.backend.search("均熱板", aux_terms=["vapor chamber"])
        self.assertIn("syn_target.md", [h.node for h in hits])

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
