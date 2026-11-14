import unittest
from pathlib import Path
import tempfile

from okf_kb_agent.wiki.vocab import (
    build_vocab,
    damerau_levenshtein_distance,
    suggest_correction,
)


class TestVocab(unittest.TestCase):
    def test_damerau_levenshtein_distance(self):
        self.assertEqual(damerau_levenshtein_distance("vapor", "vapor"), 0)
        # Substitution: vaper -> vapor
        self.assertEqual(damerau_levenshtein_distance("vaper", "vapor"), 1)
        # Transposition: daimond -> diamond
        self.assertEqual(damerau_levenshtein_distance("daimond", "diamond"), 1)
        # Insertion / Deletion: sinterring -> sintering
        self.assertEqual(damerau_levenshtein_distance("sinterring", "sintering"), 1)
        # Multi-edits
        self.assertEqual(damerau_levenshtein_distance("sintered", "sintering"), 3)

    def test_suggest_correction_targets(self):
        vocab = {"vapor", "diamond", "sintering", "copper", "chamber"}
        self.assertEqual(suggest_correction("vaper", vocab), "vapor")
        self.assertEqual(suggest_correction("daimond", vocab), "diamond")
        self.assertEqual(suggest_correction("sinterring", vocab), "sintering")

    def test_suggest_correction_short_word_guard(self):
        vocab = {"gan", "sic", "tim", "gas", "six", "tin", "vapor"}
        # len <= 3: never expand even if distance == 1
        self.assertIsNone(suggest_correction("gan", vocab))
        self.assertIsNone(suggest_correction("sic", vocab))
        self.assertIsNone(suggest_correction("tim", vocab))
        self.assertIsNone(suggest_correction("ga", vocab))
        self.assertIsNone(suggest_correction("gann", vocab))  # len 4 -> distance 1 to gan? Wait, gann len=4 to gan len=3 dist=1, but target gan len <= 3? Let's check rule!

    def test_suggest_correction_length_rules(self):
        vocab = {"copper", "solder", "chamber", "diamond"}
        # Word already in vocab: no correction
        self.assertIsNone(suggest_correction("copper", vocab))

        # Length 4-5: dist <= 1 allowed, dist >= 2 forbidden
        # "sopp" len 4, distance to copper is 3 -> None
        self.assertIsNone(suggest_correction("sopp", vocab))
        # "soldr" len 5, distance to solder is 1 -> solder
        self.assertEqual(suggest_correction("soldr", vocab), "solder")

        # Length >= 6: dist <= 2 allowed, dist >= 3 forbidden
        # "daimond" len 7, dist 1 to diamond -> diamond
        self.assertEqual(suggest_correction("daimond", vocab), "diamond")
        # "dimond" len 6, dist 1 to diamond -> diamond
        self.assertEqual(suggest_correction("dimond", vocab), "diamond")
        # "dimnd" len 5, dist 2 to diamond -> forbidden because query len 5 allows only dist 1
        self.assertIsNone(suggest_correction("dimnd", vocab))

    def test_build_vocab_from_wiki(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            concepts_dir = root / "concepts"
            concepts_dir.mkdir(parents=True)

            note = concepts_dir / "vapor-chamber.md"
            note.write_text(
                "---\ntitle: \"ベイパーチャンバー（Vapor Chamber）\"\n"
                "tags: [vapor-chamber, heat-pipe, cooling]\n---\n"
                "# Vapor Chamber Technology\n\n## Working Fluid\nWater is used.\n",
                encoding="utf-8"
            )

            syn_file = root / "synonyms.json"
            syn_file.write_text('{"copper": ["Cu", "sintered copper"]}', encoding="utf-8")

            vocab = build_vocab(root)
            self.assertIn("vapor", vocab)
            self.assertIn("chamber", vocab)
            self.assertIn("vapor-chamber", vocab)
            self.assertIn("cooling", vocab)
            self.assertIn("copper", vocab)
            self.assertIn("cu", vocab)
            self.assertIn("sintered", vocab)


if __name__ == "__main__":
    unittest.main()
