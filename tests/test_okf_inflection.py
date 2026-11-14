import unittest
from pathlib import Path
import tempfile

from okf_kb_agent.wiki.search import LocalSearchBackend, strip_suffix


class TestInflectionSearch(unittest.TestCase):
    def test_strip_suffix(self):
        # -s / -es / -ed / -ing
        self.assertEqual(strip_suffix("chambers"), "chamber")
        self.assertEqual(strip_suffix("interfaces"), "interface")
        self.assertEqual(strip_suffix("sintered"), "sinter")
        self.assertEqual(strip_suffix("sintering"), "sinter")

        # -er must NOT be stripped (solder, copper guard)
        self.assertIsNone(strip_suffix("solder"))
        self.assertIsNone(strip_suffix("copper"))
        self.assertIsNone(strip_suffix("spreader"))

        # Stem length >= 4 guard
        self.assertIsNone(strip_suffix("dogs"))  # dog len 3 < 4
        self.assertIsNone(strip_suffix("ties"))  # ti len 2 < 4
        self.assertIsNone(strip_suffix("sing"))  # s len 1 < 4
        self.assertIsNone(strip_suffix("beds"))  # bed len 3 < 4
        self.assertIsNone(strip_suffix("pass"))  # ss guard

    def test_inflection_probes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            concepts_dir = root / "concepts"
            concepts_dir.mkdir(parents=True)

            # F-1 concept: silver sintering
            (concepts_dir / "silver-sintering.md").write_text(
                "---\ntitle: \"焼結銀（Silver Sintering）\"\n"
                "tags: [silver-sintering, die-attach]\ndescription: \"Silver sintering joint\"\n---\n"
                "# Silver Sintering\nSilver sintering process.\n",
                encoding="utf-8"
            )
            # F-2 concept: copper sintering
            (concepts_dir / "copper-sintering.md").write_text(
                "---\ntitle: \"銅焼結（Copper Sintering）\"\n"
                "tags: [copper-sintering, sintered-copper]\ndescription: \"Copper sintering technology\"\n---\n"
                "# Copper Sintering\nCopper sintering process.\n",
                encoding="utf-8"
            )
            # F-3 concept: vapor chamber
            (concepts_dir / "vapor-chamber.md").write_text(
                "---\ntitle: \"ベイパーチャンバー（Vapor Chamber）\"\n"
                "tags: [vapor-chamber, cooling]\ndescription: \"Two-phase device\"\n---\n"
                "# Vapor Chamber\nVapor chamber cooling.\n",
                encoding="utf-8"
            )

            backend = LocalSearchBackend(root, use_ripgrep=False)

            # F-1: "silver sintered" should hit silver-sintering
            hits_f1 = backend.search("silver sintered")
            self.assertTrue(len(hits_f1) > 0)
            self.assertEqual(hits_f1[0].node, "concepts/silver-sintering.md")

            # F-2: "sintered copper" should hit copper-sintering
            hits_f2 = backend.search("sintered copper")
            self.assertTrue(len(hits_f2) > 0)
            self.assertEqual(hits_f2[0].node, "concepts/copper-sintering.md")

            # F-3: "vapor chambers" should hit vapor-chamber
            hits_f3 = backend.search("vapor chambers")
            self.assertTrue(len(hits_f3) > 0)
            self.assertEqual(hits_f3[0].node, "concepts/vapor-chamber.md")


if __name__ == "__main__":
    unittest.main()
