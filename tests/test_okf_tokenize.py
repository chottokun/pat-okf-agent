from __future__ import annotations

import pytest

from pat_okf_agent.wiki.tokenize import tokenize


def test_tokenize_alnum_and_units():
    text = "vapor-chamber 2000 W/mK 7-9 ppm/K 1 kW/cm2 AlSiC"
    tokens = tokenize(text)
    assert "vapor-chamber" in tokens
    assert "2000" in tokens
    assert "w/mk" in tokens
    assert "7-9" in tokens
    assert "ppm/k" in tokens
    assert "1" in tokens
    assert "kw/cm2" in tokens
    assert "alsic" in tokens


def test_tokenize_nfkc_normalization():
    text = "ＡｌＳｉＣ Ｗ／ｍＫ サーバー"
    tokens = tokenize(text)
    assert "alsic" in tokens
    assert "w/mk" in tokens
    assert "サーバー" in tokens


def test_tokenize_katakana():
    tokens = tokenize("ベースプレート")
    assert "ベースプレート" in tokens
    assert "ベー" in tokens
    assert "ース" in tokens
    assert "スプ" in tokens
    assert "プレ" in tokens
    assert "レー" in tokens
    assert "ート" in tokens


def test_tokenize_cjk_bigram():
    tokens = tokenize("銀焼結")
    assert tokens == ["銀焼", "焼結"]

    # 焼結銀との共通 bi-gram
    doc_tokens = tokenize("焼結銀")
    assert "焼結" in doc_tokens
    assert set(tokens) & set(doc_tokens) == {"焼結"}


def test_tokenize_cjk_single_char():
    # 記号や空白で区切られた1文字 CJK（「銀」「銅」「熱」など）
    tokens = tokenize("銀・銅・熱")
    assert tokens == ["銀", "銅", "熱"]

    assert tokenize("銀") == ["銀"]
    assert tokenize("銅") == ["銅"]
    assert tokenize("熱") == ["熱"]


def test_tokenize_stopwords_removal():
    tokens = tokenize("これはテストです。それから")
    # "です", "から" などのストップ語
    assert "は" not in tokens
    assert "です" not in tokens
    assert "から" not in tokens
    assert "テスト" in tokens

    # 単独の助詞・機能語
    assert tokenize("は") == []
    assert tokenize("が") == []
    assert tokenize("の") == []
    assert tokenize("です") == []


def test_tokenize_example_1():
    text = "熱膨張係数が 7から9 ppm/K でSiに近い AlSiC のベースプレート"
    tokens = tokenize(text)
    assert "熱膨" in tokens
    assert "膨張" in tokens
    assert "張係" in tokens
    assert "係数" in tokens
    assert "7" in tokens
    assert "9" in tokens
    assert "ppm/k" in tokens
    assert "si" in tokens
    assert "alsic" in tokens
    assert "ベースプレート" in tokens
    assert "ベー" in tokens
    # ストップ語「から」は除外
    assert "から" not in tokens


def test_tokenize_example_3():
    text = "サーバーを冷媒に丸ごと沈めて除熱する方式"
    tokens = tokenize(text)
    assert "サーバー" in tokens
    assert "サー" in tokens
    assert "冷媒" in tokens
    assert "除熱" in tokens
    assert "方式" in tokens
