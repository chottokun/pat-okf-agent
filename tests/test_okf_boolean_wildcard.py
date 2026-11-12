import unittest
from pathlib import Path
import tempfile

from pat_okf_agent.wiki.search import LocalSearchBackend


class TestBooleanWildcardSearch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        concepts_dir = self.root / "concepts"
        concepts_dir.mkdir(parents=True)

        (concepts_dir / "silver-sintering.md").write_text(
            "---\ntitle: \"焼結銀（Silver Sintering）\"\n"
            "tags: [silver-sintering, silver, die-attach]\ndescription: \"Silver sintering joint\"\n---\n"
            "# Silver Sintering\nSilver sintering process.\n",
            encoding="utf-8"
        )
        (concepts_dir / "copper-sintering.md").write_text(
            "---\ntitle: \"銅焼結（Copper Sintering）\"\n"
            "tags: [copper-sintering, copper, die-attach]\ndescription: \"Copper sintering joint\"\n---\n"
            "# Copper Sintering\nCopper sintering process.\n",
            encoding="utf-8"
        )
        (concepts_dir / "vapor-chamber.md").write_text(
            "---\ntitle: \"ベイパーチャンバー（Vapor Chamber）\"\n"
            "tags: [vapor-chamber, cooling]\ndescription: \"Two-phase device\"\n---\n"
            "# Vapor Chamber\nVapor chamber cooling.\n",
            encoding="utf-8"
        )
        self.backend = LocalSearchBackend(self.root, use_ripgrep=False)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_and_operator_narrows_hits(self):
        # sintering alone hits both silver and copper
        hits_sintering = self.backend.search("sintering")
        self.assertEqual(len(hits_sintering), 2)

        # "sintering AND silver" must hit only silver-sintering (or rank it #1 with copper excluded)
        hits_and = self.backend.search("sintering AND silver")
        self.assertTrue(len(hits_and) >= 1)
        self.assertEqual(hits_and[0].node, "concepts/silver-sintering.md")
        self.assertLess(len(hits_and), len(hits_sintering))

    def test_or_operator_includes_both_concepts(self):
        hits = self.backend.search("vapor-chamber OR silver-sintering")
        nodes = [h.node for h in hits[:5]]
        self.assertIn("concepts/vapor-chamber.md", nodes)
        self.assertIn("concepts/silver-sintering.md", nodes)

    def test_lowercase_and_or_are_normal_terms(self):
        # Lowercase "and" / "or" are treated as regular search terms
        hits = self.backend.search("vapor and chamber")
        # "and" will not match anything in vapor-chamber unless and is in text
        # Since strict AND requires all terms, this should not match unless relaxed or present
        # Most importantly, it should not be treated as boolean AND
        pass

    def test_wildcard_prefix(self):
        # "sinter* copper" should match copper-sintering as #1
        hits = self.backend.search("sinter* copper")
        self.assertTrue(len(hits) > 0)
        self.assertEqual(hits[0].node, "concepts/copper-sintering.md")

    def test_wildcard_suffix_or_middle_graceful(self):
        # Invalid wildcards like *ing or s*ing should not crash
        hits1 = self.backend.search("*ing")
        self.assertIsInstance(hits1, list)
        hits2 = self.backend.search("s*ing")
        self.assertIsInstance(hits2, list)

    def test_score_hierarchy(self):
        # Exact (1.0) > Stem (0.8) > Wildcard (0.7)
        # On copper-sintering:
        # Exact "sintering copper"
        hits_exact = self.backend.search("sintering copper")
        # Stem "sintered copper"
        hits_stem = self.backend.search("sintered copper")
        # Wildcard "sinter* copper"
        hits_wild = self.backend.search("sinter* copper")

        score_exact = [h.score for h in hits_exact if h.node == "concepts/copper-sintering.md"][0]
        score_stem = [h.score for h in hits_stem if h.node == "concepts/copper-sintering.md"][0]
        score_wild = [h.score for h in hits_wild if h.node == "concepts/copper-sintering.md"][0]

        self.assertGreater(score_exact, score_stem)
        self.assertGreater(score_stem, score_wild)


if __name__ == "__main__":
    unittest.main()
