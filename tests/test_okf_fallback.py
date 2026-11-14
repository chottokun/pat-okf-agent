import unittest
from pathlib import Path
import tempfile

from okf_kb_agent.wiki.search import LocalSearchBackend, SearchHit


class TestFallbackSearch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        concepts_dir = self.root / "concepts"
        concepts_dir.mkdir(parents=True)

        # Create sample concept: vapor chamber
        (concepts_dir / "vapor-chamber.md").write_text(
            "---\ntitle: \"ベイパーチャンバー（Vapor Chamber）\"\n"
            "tags: [vapor-chamber, heat-pipe, cooling]\n---\n"
            "# Vapor Chamber Overview\n\nHigh thermal conductivity two-phase device.\n",
            encoding="utf-8"
        )
        # Create sample concept: silver sintering
        (concepts_dir / "silver-sintering.md").write_text(
            "---\ntitle: \"焼結銀（Silver Sintering）\"\n"
            "tags: [silver-sintering, die-attach]\n---\n"
            "# Silver Sintering Technology\n\nHigh temperature die-attach material.\n",
            encoding="utf-8"
        )
        self.backend = LocalSearchBackend(self.root, use_ripgrep=False)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_normal_hit_no_fallback(self):
        hits = self.backend.search("vapor chamber")
        self.assertTrue(len(hits) > 0)
        self.assertFalse(hits[0].is_fallback)
        self.assertIsNone(hits[0].query_corrections)

    def test_single_term_typo_fallback(self):
        # "sinterring" single term typo -> should correct to "sintering" and hit silver-sintering
        hits = self.backend.search("sinterring")
        self.assertTrue(len(hits) > 0)
        self.assertTrue(hits[0].is_fallback)
        self.assertEqual(hits[0].query_corrections, {"sinterring": "sintering"})
        self.assertEqual(hits[0].node, "concepts/silver-sintering.md")

    def test_multi_term_typo_fallback(self):
        # "vaper chamber" multi-term typo -> should correct "vaper" -> "vapor"
        hits = self.backend.search("vaper chamber")
        self.assertTrue(len(hits) > 0)
        self.assertTrue(hits[0].is_fallback)
        self.assertEqual(hits[0].query_corrections, {"vaper": "vapor"})
        self.assertEqual(hits[0].node, "concepts/vapor-chamber.md")

    def test_japanese_zero_hit_no_fallback(self):
        # Japanese query with 0 hit should return empty without fallback
        hits = self.backend.search("存在しない未知の架空概念語")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
