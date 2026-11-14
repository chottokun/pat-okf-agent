"""KB vocabulary extraction and spelling correction module.

Extracts ALNUM vocabulary from OKF concept notes and synonyms.json,
and provides Damerau-Levenshtein distance matching with length guards.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable

from .model import iter_concepts
from .tokenize import tokenize

__all__ = [
    "build_vocab",
    "damerau_levenshtein_distance",
    "extract_alnum_terms",
    "suggest_correction",
]

_ALNUM_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def damerau_levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Damerau-Levenshtein distance (with transposition) between two strings."""
    len1 = len(s1)
    len2 = len(s2)
    d: dict[tuple[int, int], int] = {}

    for i in range(-1, len1 + 1):
        d[(i, -1)] = i + 1
    for j in range(-1, len2 + 1):
        d[(-1, j)] = j + 1

    for i in range(len1):
        for j in range(len2):
            cost = 0 if s1[i] == s2[j] else 1
            d[(i, j)] = min(
                d[(i - 1, j)] + 1,        # deletion
                d[(i, j - 1)] + 1,        # insertion
                d[(i - 1, j - 1)] + cost, # substitution
            )
            if i > 0 and j > 0 and s1[i] == s2[j - 1] and s1[i - 1] == s2[j]:
                d[(i, j)] = min(d[(i, j)], d[(i - 2, j - 2)] + 1)  # transposition

    return d[(len1 - 1, len2 - 1)]


def extract_alnum_terms(text: str) -> set[str]:
    """Extract lowercase ALNUM terms from text, including hyphen/underscore sub-tokens."""
    if not text:
        return set()
    terms: set[str] = set()
    for match in _ALNUM_PATTERN.finditer(text):
        token = match.group(0).casefold()
        terms.add(token)
        if "-" in token or "_" in token or "/" in token or "." in token:
            for sub in re.split(r"[-_/.]+", token):
                if sub:
                    terms.add(sub)
    return terms


def build_vocab(root: str | Path) -> set[str]:
    """Extract ALNUM vocabulary from concept titles, tags, headings, and synonyms.json."""
    vocab: set[str] = set()
    root_path = Path(root)

    # 1. From concept notes
    for concept in iter_concepts(root_path):
        vocab.update(extract_alnum_terms(concept.title))
        for tag in concept.tags:
            vocab.update(extract_alnum_terms(tag))
        if concept.description:
            vocab.update(extract_alnum_terms(concept.description))
        for match in _HEADING_RE.finditer(concept.body):
            vocab.update(extract_alnum_terms(match.group(1)))

    # 2. From synonyms.json if present
    syn_path = root_path / "synonyms.json"
    if syn_path.is_file():
        try:
            with open(syn_path, "r", encoding="utf-8") as f:
                syn_data = json.load(f)
            if isinstance(syn_data, dict):
                for key, val in syn_data.items():
                    vocab.update(extract_alnum_terms(str(key)))
                    if isinstance(val, list):
                        for item in val:
                            vocab.update(extract_alnum_terms(str(item)))
                    elif isinstance(val, str):
                        vocab.update(extract_alnum_terms(val))
        except (OSError, json.JSONDecodeError):
            pass

    return vocab


def suggest_correction(word: str, vocab: Iterable[str]) -> str | None:
    """Suggest the closest vocabulary word for an ALNUM word using Damerau-Levenshtein.

    Rules & Guards:
    - If word is already in vocab, returns None (no correction needed).
    - If len(word) <= 3 or target len <= 3, returns None (short-word guard).
    - If len(word) in (4, 5): allowed distance is 1 only.
    - If len(word) >= 6: allowed distance is <= 2.
    - Deterministic tie-breaking: (distance, length_diff, target).
    """
    if not word:
        return None

    w_lower = word.casefold()
    vocab_set = set(vocab) if not isinstance(vocab, set) else vocab

    if w_lower in vocab_set:
        return None

    w_len = len(w_lower)
    if w_len <= 3:
        return None

    max_dist = 1 if w_len <= 5 else 2

    candidates: list[tuple[int, int, str]] = []
    for cand in vocab_set:
        c_len = len(cand)
        if c_len <= 3:
            continue
        if abs(w_len - c_len) > max_dist:
            continue
        dist = damerau_levenshtein_distance(w_lower, cand)
        if dist <= max_dist:
            candidates.append((dist, abs(w_len - c_len), cand))

    if not candidates:
        return None

    # Sort deterministically: lowest distance, smallest length diff, alphabetical
    candidates.sort()
    return candidates[0][2]
