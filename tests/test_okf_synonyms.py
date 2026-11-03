"""Tests for bounded synonym expansion and retrieval evaluation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.search import LocalSearchBackend, pattern_for
from pat_okf_agent.wiki.synonyms import (
    ExpandedSearchBackend,
    SynonymIndex,
    evaluate_synonyms,
    load_eval_queries,
    pattern_reason,
)


def write(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


class SynonymTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "a.md", "type: Note\ntitle: Glass note\n",
              "# Glass note\nMetallic glass cools rapidly.\n")
        self.index = SynonymIndex.from_data([
            ["amorphous alloy", "metallic glass", "アモルファス合金"],
            ["claim", "クレーム"],
        ])

    def test_validation_and_expansion_are_bounded(self):
        self.assertEqual(self.index.expand_query("metallic glass film")[0],
                         "metallic glass film")
        variants = self.index.expand_query("metallic glass film")
        self.assertIn("amorphous alloy film", variants)
        self.assertIn("アモルファス合金 film", variants)
        many = self.index.expand_query("metallic glass claim", max_variants=3)
        self.assertLessEqual(len(many), 3)
        with self.assertRaises(ValueError):
            SynonymIndex.from_data([["one"]])
        with self.assertRaises(ValueError):
            SynonymIndex.from_data([["same", "same"]])
        with self.assertRaises(ValueError):
            SynonymIndex.from_data([["a", "b"], ["A", "c"]])
        with self.assertRaises(ValueError):
            self.index.expand_query("x", max_variants=0)
        boundary = SynonymIndex.from_data([["art", "芸術"]])
        self.assertEqual(boundary.expand_query("part"), ["part"])
        self.assertEqual(boundary.expand_query("art"), ["art", "芸術"])

    def test_expanded_backend_recovers_synonym_query(self):
        backend = LocalSearchBackend(self.root)
        self.assertEqual(backend.search("amorphous alloy"), [])
        expanded = ExpandedSearchBackend(backend, self.index)
        hits = expanded.search("amorphous alloy")
        self.assertEqual([hit.node for hit in hits], ["a.md"])
        self.assertEqual(expanded.search_keywords("amorphous alloy", top_k=1),
                         ["a.md"])

    def test_evaluate_measures_failure_and_recommends_pattern_c(self):
        backend = LocalSearchBackend(self.root)
        queries = [
            {"id": "syn-1", "query": "amorphous alloy",
             "expected": ["a.md"]},
            {"id": "syn-2", "query": "クレーム",
             "expected": ["missing.md"]},
        ]
        result = evaluate_synonyms(
            self.root, backend, SynonymIndex.from_data([[
                "amorphous alloy", "metallic glass"]]),
            queries=queries, limit=3)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["baseline_failures"], 2)
        self.assertEqual(result["failures"], 1)
        self.assertAlmostEqual(result["failure_rate"], 0.5)
        self.assertEqual(result["recommended_pattern"], "C")
        self.assertIn("15%", result["pattern_reason"])
        self.assertEqual(result["rows"][0]["baseline_ok"], False)

    def test_query_file_loading_and_pattern_threshold(self):
        path = self.root / "eval_queries.json"
        path.write_text(json.dumps({"queries": [
            {"query": "metallic glass", "expected": ["./a.md"]},
        ]}), encoding="utf-8")
        queries = load_eval_queries(path)
        self.assertEqual(queries[0]["expected"], ["a.md"])
        self.assertEqual(pattern_for(10, 0.15), "A")
        self.assertEqual(pattern_for(10, 0.16), "C")
        self.assertEqual(pattern_for(2001, 0.0), "C")
        with self.assertRaises(ValueError):
            pattern_for(10, 1.1)
        self.assertIn("above 500", pattern_reason(600, 0.01))
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_eval_queries(path)


if __name__ == "__main__":
    unittest.main()
