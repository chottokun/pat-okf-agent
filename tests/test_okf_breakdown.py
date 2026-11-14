"""Tests for search hit breakdown (cause-and-effect scoring audit)."""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from okf_kb_agent.wiki.search import (
    LocalSearchBackend,
    SearchHit,
)
from okf_kb_agent.wiki.synonyms import ExpandedSearchBackend, SynonymIndex


def write_doc(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


class BreakdownU1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # 1. exact & phrase match
        write_doc(
            self.root,
            "concepts/sintered-copper.md",
            "type: Concept\ntitle: Sintered Copper Paste\ntags: [copper, sintering]\ndescription: High thermal conductivity paste.\n",
            "## Sintered Copper Overview\nLead text mentioning sintered copper technology.\nDetailed body text about copper sintering.\n",
        )
        # 2. stem match (sintering -> stem 'sinter')
        write_doc(
            self.root,
            "concepts/copper-sintering.md",
            "type: Concept\ntitle: Copper Sintering Process\ntags: [sinter, metallurgy]\ndescription: Process details.\n",
            "## Process Guidelines\nSome lead text.\nBody text containing sintering and copper.\n",
        )
        # 3. body saturation cap (>10 occurrences) & hl cap
        write_doc(
            self.root,
            "concepts/cap-test.md",
            "type: Concept\ntitle: Thermal Device\ntags: [cooling]\ndescription: General cooling device.\n",
            "## Cooling Headings Lead 1\n## Cooling Headings Lead 2\n## Cooling Headings Lead 3\nCooling lead content.\n"
            + ("cooling " * 20) + "\n",
        )
        # 4. deprecated note
        write_doc(
            self.root,
            "concepts/deprecated-device.md",
            "type: Concept\ntitle: Obsolete Heat Pipe\ntags: [deprecated, heat-pipe]\ndescription: Legacy cooling device.\n",
            "Body mentioning heat pipe.\n",
        )
        # 5. Japanese bigram match
        write_doc(
            self.root,
            "concepts/japanese-doc.md",
            "type: Concept\ntitle: 高熱伝導 放熱シート\ntags: [放熱, シート]\ndescription: 高熱伝導シートの特性。\n",
            "## 概要\n放熱シートの説明。\n本文にも放熱シートが登場する。\n",
        )
        # 6. Vocab note for typo correction (e.g. 'sintered' -> 'sintered')
        self.backend = LocalSearchBackend(self.root)

    def _assert_invariant(self, hit: SearchHit):
        self.assertIsNotNone(hit.breakdown, f"Hit {hit.node} should have breakdown")
        bd = hit.breakdown
        phrase_pts = bd["phrase"]["points"]
        fields_pts = sum(bd["fields"].values())
        adj_pts = sum(adj["points"] for adj in bd["adjustments"])
        recalculated = phrase_pts + fields_pts + adj_pts
        self.assertTrue(
            math.isclose(recalculated, bd["total"], abs_tol=1e-9),
            f"Invariant failed for {hit.node}: recalculated {recalculated} != total {bd['total']}",
        )
        self.assertTrue(
            math.isclose(bd["total"], hit.score, abs_tol=1e-9),
            f"Total mismatch for {hit.node}: breakdown total {bd['total']} != hit.score {hit.score}",
        )

    def test_search_hit_default_breakdown_is_none(self):
        hit = SearchHit(node="a.md", score=10.0, title="T", snippet="S", matched_terms=1)
        self.assertIsNone(hit.breakdown)

    def test_exact_and_phrase_breakdown_structure(self):
        hits = self.backend.search("sintered copper")
        self.assertGreaterEqual(len(hits), 1)
        top = hits[0]
        self._assert_invariant(top)
        bd = top.breakdown

        # terms check
        term_map = {t["term"]: t for t in bd["terms"]}
        self.assertIn("sintered", term_map)
        self.assertIn("copper", term_map)
        self.assertEqual(term_map["sintered"]["role"], "query")

        # phrase check
        self.assertEqual(bd["phrase"]["text"], "sintered copper")
        self.assertGreater(bd["phrase"]["points"], 0.0)

        # gate check
        self.assertEqual(bd["gate"]["rule"], "and")
        self.assertIn("sintered", bd["gate"]["matched"])
        self.assertIn("copper", bd["gate"]["matched"])
        self.assertEqual(bd["gate"]["missed"], [])

    def test_stem_match_kind_and_factor(self):
        hits = self.backend.search("sintering")
        hit_stem = next((h for h in hits if h.node == "concepts/copper-sintering.md"), None)
        self.assertIsNotNone(hit_stem)
        self._assert_invariant(hit_stem)

        bd = hit_stem.breakdown
        # tags has "sinter" which is the stem for "sintering", or title "copper sintering process"
        term_entry = bd["terms"][0]
        self.assertEqual(term_entry["term"], "sintering")
        # Find match with stem kind
        stem_matches = [m for m in term_entry["matches"] if m["kind"] == "stem"]
        self.assertTrue(len(stem_matches) > 0)
        for sm in stem_matches:
            self.assertEqual(sm["factor"], 0.8)

    def test_wildcard_kind_and_factor(self):
        hits = self.backend.search("sinter*")
        self.assertGreaterEqual(len(hits), 1)
        for h in hits:
            self._assert_invariant(h)
            bd = h.breakdown
            wc_matches = [
                m for t in bd["terms"] for m in t["matches"] if m["kind"] == "wildcard"
            ]
            self.assertTrue(len(wc_matches) > 0)
            for wm in wc_matches:
                self.assertEqual(wm["factor"], 0.7)

    def test_body_and_hl_caps_adjustments(self):
        hits = self.backend.search("cooling")
        cap_hit = next((h for h in hits if h.node == "concepts/cap-test.md"), None)
        self.assertIsNotNone(cap_hit)
        self._assert_invariant(cap_hit)

        bd = cap_hit.breakdown
        adj_kinds = [a["kind"] for a in bd["adjustments"]]
        self.assertIn("cap", adj_kinds)

        cap_fields = [a["field"] for a in bd["adjustments"] if a["kind"] == "cap"]
        self.assertIn("body", cap_fields)
        body_cap_adj = next(a for a in bd["adjustments"] if a.get("field") == "body")
        self.assertLess(body_cap_adj["points"], 0.0)
        self.assertEqual(bd["fields"]["body"] + body_cap_adj["points"], 10.0)

    def test_deprecated_attenuation_adjustment(self):
        hits = self.backend.search("heat pipe")
        dep_hit = next((h for h in hits if h.node == "concepts/deprecated-device.md"), None)
        self.assertIsNotNone(dep_hit)
        self._assert_invariant(dep_hit)

        bd = dep_hit.breakdown
        dep_adjs = [a for a in bd["adjustments"] if a["kind"] == "deprecated_attenuation"]
        self.assertEqual(len(dep_adjs), 1)
        self.assertEqual(dep_adjs[0]["factor"], 0.3)
        self.assertLess(dep_adjs[0]["points"], 0.0)

    def test_japanese_bigram_match(self):
        hits = self.backend.search("放熱シート")
        self.assertGreaterEqual(len(hits), 1)
        for h in hits:
            self._assert_invariant(h)
            bd = h.breakdown
            # "放熱" and "シート" or bigrams
            kinds = [m["kind"] for t in bd["terms"] for m in t["matches"]]
            self.assertTrue(any(k == "bigram" for k in kinds))

    def test_synonym_aux_breakdown(self):
        syn_index = SynonymIndex(groups=(("copper paste", "cu paste"),))
        expanded = ExpandedSearchBackend(self.backend, syn_index)
        hits = expanded.search("copper paste")
        self.assertGreaterEqual(len(hits), 1)
        for h in hits:
            self._assert_invariant(h)

    def test_typo_correction_breakdown(self):
        # "sinterd" typo for "sintered"
        hits = self.backend.search("sinterd copper")
        self.assertGreaterEqual(len(hits), 1)
        top = hits[0]
        self._assert_invariant(top)
        bd = top.breakdown
        self.assertEqual(bd["gate"]["rule"], "fallback_corrections")
        aux_terms = [t for t in bd["terms"] if t["role"] == "aux"]
        self.assertTrue(len(aux_terms) > 0)
        for at in aux_terms:
            self.assertEqual(at["source"], "correction")
            self.assertEqual(at["corrected_from"], "sinterd")
            for m in at["matches"]:
                self.assertEqual(m["kind"], "correction")
                self.assertEqual(m["factor"], 0.5)


class BreakdownU2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_doc(
            self.root,
            "concepts/copper-sintering.md",
            "type: Concept\ntitle: Copper Sintering Process\ntags: [copper, sintering]\ndescription: Process details.\n",
            "## Process Guidelines\nLead text mentioning copper sintering.\nDetailed body text.\n",
        )
        self.backend = LocalSearchBackend(self.root)

    def test_as_dict_without_breakdown_omits_key(self):
        hit = SearchHit(node="a.md", score=12.3456, title="T", snippet="S", matched_terms=1, breakdown=None)
        d = hit.as_dict()
        self.assertNotIn("breakdown", d)
        self.assertEqual(d["score"], 12.346)

    def test_as_dict_with_breakdown_rounds_floats_and_matches_score(self):
        import json
        hits = self.backend.search("copper sintering")
        self.assertGreaterEqual(len(hits), 1)
        hit = hits[0]
        self.assertIsNotNone(hit.breakdown)
        d = hit.as_dict()
        self.assertIn("breakdown", d)

        # JSON dump & load round-trip
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        bd = parsed["breakdown"]

        # Check total matches rounded score
        self.assertEqual(bd["total"], parsed["score"])

        # Check components sum matches score within 0.01
        phrase_pts = bd["phrase"]["points"]
        fields_pts = sum(bd["fields"].values())
        adj_pts = sum(adj["points"] for adj in bd["adjustments"])
        components_sum = phrase_pts + fields_pts + adj_pts
        self.assertAlmostEqual(components_sum, parsed["score"], delta=0.01)

        # Check float rounding: all floats in bd should have at most 3 decimal places
        def _check_floats(obj):
            if isinstance(obj, float):
                # string representation after round(x, 3) should have at most 3 decimals
                s = str(obj)
                if "." in s:
                    dec_len = len(s.split(".")[1])
                    # Note: float representation might be like 12.345 or 12.3
                    self.assertLessEqual(dec_len, 4)  # 3 decimals + possible float repr artifact
            elif isinstance(obj, dict):
                for v in obj.values():
                    _check_floats(v)
            elif isinstance(obj, list):
                for item in obj:
                    _check_floats(item)

        _check_floats(bd)

    def test_cli_text_output_unchanged(self):
        import io
        from unittest.mock import patch
        from okf_kb_agent.wiki.cli import main

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ret = main(["search", str(self.root), "copper sintering"])
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        # Should have "<score>  <node>  <title>"
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertTrue(len(lines) >= 1)
        first_line = lines[0]
        self.assertIn("concepts/copper-sintering.md", first_line)
        self.assertIn("Copper Sintering Process", first_line)
        self.assertNotIn("breakdown", output)


class BreakdownU3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Note matching 'copper'
        write_doc(
            self.root,
            "concepts/copper.md",
            "type: Concept\ntitle: Copper Substrate\ntags: [copper]\ndescription: Copper desc.\n",
            "Body copper.\n",
        )
        self.backend = LocalSearchBackend(self.root)
        self.syn_index = SynonymIndex(groups=(("aluminum", "al"),))

    def test_eval_synonyms_failure_detail(self):
        from okf_kb_agent.wiki.synonyms import evaluate_synonyms

        # Query 1: success (matches concepts/copper.md)
        # Query 2: failure (unrelated expected or unretrieved)
        queries = [
            {"id": "q1", "query": "copper", "expected": ["concepts/copper.md"]},
            {"id": "q2", "query": "copper", "expected": ["concepts/nonexistent.md"]},
        ]
        result = evaluate_synonyms(self.root, self.backend, self.syn_index, queries=queries, limit=5)
        rows = result["rows"]
        self.assertEqual(len(rows), 2)

        # q1: success row -> no failure_detail
        self.assertTrue(rows[0]["expanded_ok"])
        self.assertNotIn("failure_detail", rows[0])

        # q2: failure row -> has failure_detail
        self.assertFalse(rows[1]["expanded_ok"])
        self.assertIn("failure_detail", rows[1])
        fd = rows[1]["failure_detail"]
        self.assertIn("expanded_top_hit", fd)
        self.assertIn("baseline_top_hit", fd)

        exp_top = fd["expanded_top_hit"]
        base_top = fd["baseline_top_hit"]
        self.assertIsNotNone(exp_top)
        self.assertIsNotNone(base_top)
        self.assertIn("breakdown", exp_top)
        self.assertEqual(exp_top["breakdown"]["total"], exp_top["score"])
        self.assertIn("breakdown", base_top)
        self.assertEqual(base_top["breakdown"]["total"], base_top["score"])


if __name__ == "__main__":
    unittest.main()
