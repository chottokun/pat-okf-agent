"""Tokenization for OKF search.

A stdlib-only character-type hybrid tokenizer designed for mixed
English/alphanumeric, Katakana, and CJK (Kanji/Hiragana) content.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["tokenize", "JAPANESE_STOPWORDS"]

_TOKEN_PATTERN = re.compile(
    r"(?P<alnum>[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*)"
    r"|(?P<katakana>[\u30a1-\u30fa\u30fc]+)"
    r"|(?P<cjk>[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f]+)"
)

# 1-character and 2-character Japanese functional stopwords / particles
JAPANESE_STOPWORDS: frozenset[str] = frozenset({
    # 1-character particles
    "は", "が", "の", "を", "に", "で", "と", "へ", "も", "や", "か", "よ", "ね",
    # 2-character particles / copulas / auxiliary verbs
    "のに", "では", "から", "より", "です", "ます", "へと", "など", "また",
    "その", "この", "あの", "どの", "され", "する", "して", "した", "ある",
    "あり", "ない", "こと", "もの", "ため", "よう", "いう", "いる", "いた",
    "れば", "なら", "のか", "かも", "ので", "たら", "ても", "でも", "って",
    "には", "とは", "ばか", "ほど", "まで", "だけ", "しか",
})


def tokenize(text: str) -> list[str]:
    """Tokenize mixed text into search terms.

    1. NFKC normalization.
    2. Segment into alphanumeric, Katakana, and CJK (Kanji/Hiragana).
    3. Generate terms:
       - ALNUM: lowercased term.
       - KATAKANA: full word + character bi-grams (if len >= 2).
       - CJK: character bi-grams (len >= 2) or single character (len == 1).
    4. Suppress Japanese stopwords.
    """
    if not text:
        return []

    norm_text = unicodedata.normalize("NFKC", text)
    tokens: list[str] = []

    for match in _TOKEN_PATTERN.finditer(norm_text):
        alnum = match.group("alnum")
        if alnum is not None:
            folded = alnum.casefold()
            tokens.append(folded)
            if "-" in folded or "_" in folded:
                for sub in re.split(r"[-_]+", folded):
                    if sub and sub not in JAPANESE_STOPWORDS:
                        tokens.append(sub)
            continue

        katakana = match.group("katakana")
        if katakana is not None:
            if katakana not in JAPANESE_STOPWORDS:
                tokens.append(katakana)
            if len(katakana) >= 2:
                for i in range(len(katakana) - 1):
                    bg = katakana[i:i + 2]
                    if bg not in JAPANESE_STOPWORDS:
                        tokens.append(bg)
            continue

        cjk = match.group("cjk")
        if cjk is not None:
            if len(cjk) == 1:
                if cjk not in JAPANESE_STOPWORDS:
                    tokens.append(cjk)
            else:
                for i in range(len(cjk) - 1):
                    bg = cjk[i:i + 2]
                    if bg not in JAPANESE_STOPWORDS:
                        tokens.append(bg)

    # Deduplicate while preserving order
    return list(dict.fromkeys(tokens))
