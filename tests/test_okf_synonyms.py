"""Tests for bounded synonym expansion and retrieval evaluation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.search import LocalSearchBackend, pattern_for
from pat_okf_agent.wiki.synonyms import (
    DEFAULT_EXCLUDE_TAGS,
    ExpandedSearchBackend,
    SynonymIndex,
    classify_tag_tier,
    evaluate_synonyms,
    extract_concept_synonym_groups,
    inventory_wiki_tags,
    load_eval_queries,
    pattern_reason,
    plan_synonyms_sync,
    sync_synonyms,
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


class SynonymSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_extracts_concept_groups_and_filters_generic_and_unowned_shared_tags(self):
        # Concept 1: AlN (has unique aliases + generic tags + shared tag '放熱基板')
        write(
            self.root,
            "concepts/aluminum-nitride.md",
            "type: Concept\n"
            "title: '窒化アルミニウム（AlN）高熱伝導セラミックス'\n"
            "tags: [AlN, aluminum-nitride, 窒化アルミニウム, セラミックス, 放熱基板, thermal-conductivity, semiconductor]\n",
            "# AlN\n",
        )
        # Concept 2: SiC (owns 'SiC' via title, shares '放熱基板' and 'パワー半導体')
        write(
            self.root,
            "concepts/silicon-carbide.md",
            "type: Concept\n"
            "title: '炭化ケイ素（SiC）と高熱伝導 SiC 材料'\n"
            "tags: [SiC, silicon-carbide, 炭化ケイ素, パワー半導体, 放熱基板, thermal-conductivity]\n",
            "# SiC\n",
        )
        # Concept 3: Silver sintering (references 'SiC' as context tag, should NOT steal 'SiC')
        write(
            self.root,
            "concepts/silver-sintering.md",
            "type: Concept\n"
            "title: '焼結銀（Silver Sintering）ダイアタッチ'\n"
            "tags: [silver-sintering, 焼結銀, ダイアタッチ, パワー半導体, SiC]\n",
            "# Silver Sintering\n",
        )
        # Concept 4: Overview note with only generic tags -> < 2 terms -> skipped
        write(
            self.root,
            "concepts/semiconductor-thermal-materials.md",
            "type: Concept\n"
            "title: '半導体向け高熱伝導材料（分野概観）'\n"
            "tags: [semiconductor, thermal, thermal-conductivity, 高熱伝導, 放熱, 半導体, overview, taxonomy]\n",
            "# Overview\n",
        )
        # Non-concept nodes (Patent, Summary) must be ignored
        write(
            self.root,
            "patents/EP1.md",
            "type: Patent\ntitle: 'Pat'\ntags: [epo, patent, AlN, SiC]\n",
            "# Pat\n",
        )
        write(
            self.root,
            "SUMMARY.md",
            "type: Summary\ntitle: 'Summary'\ntags: [summary, AlN, SiC, semiconductor]\n",
            "# Summary\n",
        )

        groups = extract_concept_synonym_groups(self.root)
        self.assertEqual(
            groups,
            [
                ["aluminum-nitride", "AlN", "窒化アルミニウム"],
                ["silicon-carbide", "SiC", "炭化ケイ素"],
                ["silver-sintering", "焼結銀"],
            ],
        )
        # Validate compatibility with SynonymIndex
        idx = SynonymIndex.from_data(groups)
        self.assertEqual(len(idx.groups), 3)

    def test_canonical_owner_resolution_for_slug_initials_and_prefixes(self):
        write(
            self.root,
            "concepts/thermal-interface-materials.md",
            "type: Concept\n"
            "title: '熱界面材料（TIM / Thermal Interface Material）'\n"
            "tags: [TIM, thermal-interface-material, 熱界面材料, 放熱, semiconductor, grease, pad, phase-change]\n",
        )
        write(
            self.root,
            "concepts/phase-change-tim.md",
            "type: Concept\n"
            "title: '相変化 TIM（Phase-Change Thermal Interface Material）'\n"
            "tags: [phase-change, PCM, TIM, thermal-interface-material, 相変化, 熱界面材料, semiconductor, thermal]\n",
        )
        groups = extract_concept_synonym_groups(self.root)
        self.assertEqual(
            groups,
            [
                ["phase-change-tim", "phase-change", "PCM", "相変化"],
                ["thermal-interface-materials", "TIM", "thermal-interface-material", "熱界面材料"],
            ],
        )

    def test_sync_synonyms_is_deterministic_and_idempotent(self):
        write(
            self.root,
            "concepts/loop-heat-pipe.md",
            "type: Concept\n"
            "title: 'ループヒートパイプ（Loop Heat Pipe）'\n"
            "tags: [loop-heat-pipe, ループヒートパイプ, two-phase, lhp, capillary-wick, thermal, cooling, semiconductor, server, aerospace]\n",
        )
        first = sync_synonyms(self.root)
        self.assertTrue(first["written"])
        self.assertFalse(first["stale"])
        syn_path = self.root / "synonyms.json"
        self.assertTrue(syn_path.exists())
        raw_first = syn_path.read_bytes()

        # Check JSON key order and trailing newline
        text = raw_first.decode("utf-8")
        self.assertTrue(text.endswith("\n"))
        data = json.loads(text)
        self.assertEqual(list(data.keys()), ["groups", "generated_groups", "manual_groups"])
        self.assertEqual(data["generated_groups"], [["loop-heat-pipe", "ループヒートパイプ", "lhp"]])
        self.assertEqual(data["manual_groups"], [])
        self.assertEqual(data["groups"], [["loop-heat-pipe", "ループヒートパイプ", "lhp"]])

        # Second sync must be a no-op with identical bytes
        second = sync_synonyms(self.root)
        self.assertFalse(second["written"])
        self.assertFalse(second["stale"])
        self.assertEqual(syn_path.read_bytes(), raw_first)

        # Check mode reports stale=False
        check_res = sync_synonyms(self.root, check=True)
        self.assertFalse(check_res["written"])
        self.assertFalse(check_res["stale"])

    def test_migrates_legacy_handwritten_list_to_manual_groups(self):
        syn_path = self.root / "synonyms.json"
        syn_path.write_text(
            json.dumps([["claim", "クレーム"], ["amorphous alloy", "metallic glass"]]),
            encoding="utf-8",
        )
        write(
            self.root,
            "concepts/heat-pipe.md",
            "type: Concept\ntitle: 'ヒートパイプ'\ntags: [heat-pipe, ヒートパイプ, thermal]\n",
        )

        res = sync_synonyms(self.root)
        self.assertTrue(res["written"])
        data = json.loads(syn_path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["manual_groups"],
            [["claim", "クレーム"], ["amorphous alloy", "metallic glass"]],
        )
        self.assertEqual(
            data["generated_groups"],
            [["heat-pipe", "ヒートパイプ"]],
        )
        self.assertEqual(
            data["groups"],
            [
                ["claim", "クレーム"],
                ["amorphous alloy", "metallic glass"],
                ["heat-pipe", "ヒートパイプ"],
            ],
        )
        # SynonymIndex.from_path loads the merged groups seamlessly
        idx = SynonymIndex.from_path(syn_path)
        self.assertEqual(len(idx.groups), 3)

    def test_preserves_manual_groups_and_removes_deleted_concept_tags_on_resync(self):
        write(
            self.root,
            "concepts/alsic.md",
            "type: Concept\ntitle: 'AlSiC'\ntags: [AlSiC, aluminum-silicon-carbide, old-tag]\n",
        )
        sync_synonyms(self.root)
        syn_path = self.root / "synonyms.json"
        data = json.loads(syn_path.read_text(encoding="utf-8"))
        # User adds a manual group and a custom alias to AlSiC
        data["manual_groups"] = [
            ["custom-term", "カスタム用語"],
            ["AlSiC", "アルシック"],
        ]
        syn_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # Concept note removes 'old-tag' and adds 'アルミニウム炭化ケイ素複合材'
        write(
            self.root,
            "concepts/alsic.md",
            "type: Concept\ntitle: 'AlSiC'\ntags: [AlSiC, aluminum-silicon-carbide, アルミニウム炭化ケイ素複合材]\n",
        )
        sync_synonyms(self.root)
        updated = json.loads(syn_path.read_text(encoding="utf-8"))
        # 'old-tag' is gone from generated_groups and groups
        self.assertEqual(
            updated["generated_groups"],
            [["AlSiC", "aluminum-silicon-carbide", "アルミニウム炭化ケイ素複合材"]],
        )
        self.assertEqual(
            updated["manual_groups"],
            [["custom-term", "カスタム用語"], ["AlSiC", "アルシック"]],
        )
        # Overlapping manual group ["AlSiC", "アルシック"] absorbed unseen generated terms
        self.assertEqual(
            updated["groups"],
            [
                ["custom-term", "カスタム用語"],
                ["AlSiC", "アルシック", "aluminum-silicon-carbide", "アルミニウム炭化ケイ素複合材"],
            ],
        )
        # Validated by SynonymIndex
        idx = SynonymIndex.from_path(syn_path)
        self.assertIn("アルシック", idx.expand_query("AlSiC"))
        self.assertIn("アルミニウム炭化ケイ素複合材", idx.expand_query("AlSiC"))

    def test_sync_synonyms_preserves_tier_a_tags(self):
        write(
            self.root,
            "concepts/alsic.md",
            "type: Concept\ntitle: 'AlSiC'\ntags: [AlSiC, aluminum-silicon-carbide]\n",
        )
        sync_synonyms(self.root)
        syn_path = self.root / "synonyms.json"
        data = json.loads(syn_path.read_text(encoding="utf-8"))
        data["tier_a_tags"] = ["custom-tier-a", "alsic"]
        syn_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        sync_synonyms(self.root)
        updated = json.loads(syn_path.read_text(encoding="utf-8"))
        self.assertIn("tier_a_tags", updated)
        self.assertEqual(updated["tier_a_tags"], ["custom-tier-a", "alsic"])

    def test_classify_tag_tier(self):
        # Excluded tags -> None
        self.assertIsNone(classify_tag_tier("cooling"))
        self.assertIsNone(classify_tag_tier("thermal-management"))
        self.assertIsNone(classify_tag_tier("patent"))

        # Concept slug match -> Tier A
        self.assertEqual(
            classify_tag_tier("spray-cooling", concept_slug="spray-cooling"),
            "Tier A",
        )

        # tier_a_list match -> Tier A
        self.assertEqual(
            classify_tag_tier("direct-bond-copper", tier_a_list=["direct-bond-copper", "dbc"]),
            "Tier A",
        )

        # manual_groups match -> Tier A
        self.assertEqual(
            classify_tag_tier("al-sic", manual_groups=[["AlSiC", "al-sic"]]),
            "Tier A",
        )

        # Descriptive tag not matching any Tier A rule or exclude -> Tier B
        self.assertEqual(
            classify_tag_tier("dielectric-fluid", concept_slug="spray-cooling"),
            "Tier B",
        )

    def test_inventory_wiki_tags(self):
        write(
            self.root,
            "concepts/spray-cooling.md",
            "type: Concept\ntitle: 'Spray Cooling'\ntags: [spray-cooling, cooling, dielectric-fluid]\n",
        )
        write(
            self.root,
            "concepts/alsic.md",
            "type: Concept\ntitle: 'AlSiC'\ntags: [alsic, thermal, mmc]\n",
        )
        # index.md should be ignored
        write(
            self.root,
            "concepts/index.md",
            "type: Index\ntitle: 'Index'\ntags: [index-tag]\n",
        )

        inv = inventory_wiki_tags(self.root)
        self.assertEqual(inv["total_concepts"], 2)
        # Tags: spray-cooling, cooling, dielectric-fluid, alsic, thermal, mmc (cooling, thermal, mmc are excluded)
        self.assertIn("spray-cooling", inv["tags_by_tier"]["Tier A"])
        self.assertIn("alsic", inv["tags_by_tier"]["Tier A"])
        self.assertIn("dielectric-fluid", inv["tags_by_tier"]["Tier B"])
        self.assertIn("cooling", inv["tags_by_tier"]["Tier 外"])
        self.assertEqual(inv["tier_counts"]["Tier B"], 1)
        self.assertEqual(inv["tag_frequencies"]["dielectric-fluid"], 1)


if __name__ == "__main__":
    unittest.main()
