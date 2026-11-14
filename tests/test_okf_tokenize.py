from __future__ import annotations

import unittest

from okf_kb_agent.wiki.tokenize import tokenize


class TokenizeTests(unittest.TestCase):
    def test_tokenize_alnum_and_units(self):
        text = "vapor-chamber 2000 W/mK 7-9 ppm/K 1 kW/cm2 AlSiC"
        tokens = tokenize(text)
        self.assertIn("vapor-chamber", tokens)
        self.assertIn("2000", tokens)
        self.assertIn("w/mk", tokens)
        self.assertIn("7-9", tokens)
        self.assertIn("ppm/k", tokens)
        self.assertIn("1", tokens)
        self.assertIn("kw/cm2", tokens)
        self.assertIn("alsic", tokens)

    def test_tokenize_nfkc_normalization(self):
        text = "ＡｌＳｉＣ Ｗ／ｍＫ サーバー"
        tokens = tokenize(text)
        self.assertIn("alsic", tokens)
        self.assertIn("w/mk", tokens)
        self.assertIn("サーバー", tokens)

    def test_tokenize_katakana(self):
        tokens = tokenize("ベースプレート")
        self.assertIn("ベースプレート", tokens)
        self.assertIn("ベー", tokens)
        self.assertIn("ース", tokens)
        self.assertIn("スプ", tokens)
        self.assertIn("プレ", tokens)
        self.assertIn("レー", tokens)
        self.assertIn("ート", tokens)

    def test_tokenize_cjk_bigram(self):
        tokens = tokenize("銀焼結")
        self.assertEqual(tokens, ["銀焼", "焼結"])

        # 焼結銀との共通 bi-gram
        doc_tokens = tokenize("焼結銀")
        self.assertIn("焼結", doc_tokens)
        self.assertEqual(set(tokens) & set(doc_tokens), {"焼結"})

    def test_tokenize_cjk_single_char(self):
        tokens = tokenize("銀・銅・熱")
        self.assertEqual(tokens, ["銀", "銅", "熱"])

        self.assertEqual(tokenize("銀"), ["銀"])
        self.assertEqual(tokenize("銅"), ["銅"])
        self.assertEqual(tokenize("熱"), ["熱"])

    def test_tokenize_stopwords_removal(self):
        tokens = tokenize("これはテストです。それから")
        self.assertNotIn("は", tokens)
        self.assertNotIn("です", tokens)
        self.assertNotIn("から", tokens)
        self.assertIn("テスト", tokens)

        self.assertEqual(tokenize("は"), [])
        self.assertEqual(tokenize("が"), [])
        self.assertEqual(tokenize("の"), [])
        self.assertEqual(tokenize("です"), [])

    def test_tokenize_example_1(self):
        text = "熱膨張係数が 7から9 ppm/K でSiに近い AlSiC のベースプレート"
        tokens = tokenize(text)
        self.assertIn("熱膨", tokens)
        self.assertIn("膨張", tokens)
        self.assertIn("張係", tokens)
        self.assertIn("係数", tokens)
        self.assertIn("7", tokens)
        self.assertIn("9", tokens)
        self.assertIn("ppm/k", tokens)
        self.assertIn("si", tokens)
        self.assertIn("alsic", tokens)
        self.assertIn("ベースプレート", tokens)
        self.assertIn("ベー", tokens)
        self.assertNotIn("から", tokens)

    def test_tokenize_example_3(self):
        text = "サーバーを冷媒に丸ごと沈めて除熱する方式"
        tokens = tokenize(text)
        self.assertIn("サーバー", tokens)
        self.assertIn("サー", tokens)
        self.assertIn("冷媒", tokens)
        self.assertIn("除熱", tokens)
        self.assertIn("方式", tokens)


if __name__ == "__main__":
    unittest.main()
