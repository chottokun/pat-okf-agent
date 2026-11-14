import unittest
from pathlib import Path
import tempfile

from okf_kb_agent.wiki.search import LocalSearchBackend


class TestScoreDecay(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        concepts_dir = self.root / "concepts"
        concepts_dir.mkdir(parents=True)

        (concepts_dir / "vapor-chamber.md").write_text(
            "---\ntitle: \"ベイパーチャンバー（Vapor Chamber）\"\n"
            "tags: [vapor-chamber, heat-pipe, cooling]\ndescription: \"Two-phase vapor chamber device\"\n---\n"
            "# Vapor Chamber Overview\n\nHigh thermal conductivity vapor chamber.\n",
            encoding="utf-8"
        )
        self.backend = LocalSearchBackend(self.root, use_ripgrep=False)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_exact_score_strictly_greater_than_typo_score(self):
        # Exact match: "vapor chamber"
        exact_hits = self.backend.search("vapor chamber")
        self.assertTrue(len(exact_hits) > 0)
        exact_hit = exact_hits[0]
        self.assertFalse(exact_hit.is_fallback)

        # Typo match: "vaper chamber"
        typo_hits = self.backend.search("vaper chamber")
        self.assertTrue(len(typo_hits) > 0)
        typo_hit = typo_hits[0]
        self.assertTrue(typo_hit.is_fallback)
        self.assertEqual(typo_hit.query_corrections, {"vaper": "vapor"})

        # Absolute score inequality: Exact > Typo
        self.assertGreater(exact_hit.score, typo_hit.score)
        # Check as_dict format
        d = typo_hit.as_dict()
        self.assertTrue(d["is_fallback"])
        self.assertEqual(d["query_corrections"], {"vaper": "vapor"})


if __name__ == "__main__":
    unittest.main()
