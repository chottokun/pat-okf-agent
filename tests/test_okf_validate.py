"""Tests for OKF validation (structure, links, orphans, generated indexes)."""
from __future__ import annotations

import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pat_okf_agent.epo import PublicationRef
from pat_okf_agent.wiki.ingest import ingest_publication
from pat_okf_agent.wiki.validate import (
    STRICT_PROVENANCE_RULES,
    ValidationReport,
    validate_provenance,
    validate_wiki,
)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def node(meta: str, body: str = "# Note\n") -> str:
    return f"---\n{meta}---\n{body}"


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_valid_wiki(self):
        write(self.root, "index.md", "# Knowledge Index\n")
        write(self.root, "a.md", node("type: Note\n", "# A\n\n[b](b.md)\n"))
        write(self.root, "b.md", node("type: Note\n", "# B\n\n[a](a.md)\n"))
        report = validate_wiki(self.root)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.errors, [])

    def test_missing_type_is_an_error(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node("title: No type\n"))
        report = validate_wiki(self.root)
        self.assertFalse(report.ok)
        self.assertTrue(any("type" in e for e in report.errors))

    def test_broken_link_is_an_error(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node("type: Note\n", "[x](missing.md)\n"))
        report = validate_wiki(self.root)
        self.assertFalse(report.ok)
        self.assertTrue(any("missing.md" in e for e in report.errors))

    def test_link_escaping_root_is_an_error(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node("type: Note\n", "[x](../../x.md)\n"))
        report = validate_wiki(self.root)
        self.assertFalse(report.ok)
        self.assertTrue(any("escapes" in e for e in report.errors))

    def test_wikilink_syntax_warns_but_does_not_become_an_edge(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node(
            "type: Note\n", "# A\n\n[[b]] and [[path/b|label]]\n"))
        write(self.root, "b.md", node("type: Note\n", "# B\n"))
        report = validate_wiki(self.root)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(sum("unsupported WikiLink" in w
                             for w in report.warnings), 2)

    def test_wikilinks_in_code_are_ignored(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node(
            "type: Note\n",
            "# A\n\n`[[inline]]`\n\n```\n[[fenced]]\n```\n"))
        report = validate_wiki(self.root)
        self.assertFalse(any("unsupported WikiLink" in w
                             for w in report.warnings))

    def test_orphan_is_a_warning(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node("type: Note\n", "# A\n"))
        report = validate_wiki(self.root)
        self.assertTrue(report.ok)
        self.assertTrue(any("orphan" in w for w in report.warnings))

    def test_missing_root_index_is_a_warning(self):
        write(self.root, "a.md", node("type: Note\n", "# A\n"))
        report = validate_wiki(self.root)
        self.assertTrue(any("index.md" in w for w in report.warnings))

    def test_invalid_timestamp_and_scope_are_errors(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node(
            "type: Note\ntimestamp: yesterday\nscope: mystery\n"))
        report = validate_wiki(self.root)
        self.assertFalse(report.ok)
        self.assertTrue(any("timestamp" in e for e in report.errors))
        self.assertTrue(any("scope" in e for e in report.errors))

    def test_report_serialises(self):
        write(self.root, "index.md", "# Index\n")
        report = validate_wiki(self.root)
        data = report.as_dict()
        self.assertEqual(set(data), {"ok", "errors", "warnings",
                                     "concept_count"})
        self.assertEqual(data["concept_count"], 0)

    def test_provenance_p1_missing_basis_warns_except_stub(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node("type: Concept\n", "# A\n"))
        write(self.root, "stub.md", node("type: Patent\ntags: [epo, stub]\n", "# Stub\n"))
        report = validate_wiki(self.root)
        self.assertTrue(report.ok)
        self.assertTrue(any("[P1] missing basis" in w and "a.md" in w for w in report.warnings))
        self.assertFalse(any("[P1]" in w and "stub.md" in w for w in report.warnings))

    def test_provenance_p2_requires_sources_or_external_link(self):
        write(self.root, "index.md", "# Index\n")
        # ext_target: valid external node
        write(self.root, "ext.md", node(
            "type: Patent\nbasis: external\nsources: ['patent-biblio:EP1']\nverified_at: '2026-11-07'\n",
            "# Ext\n",
        ))
        # model_target: model basis node
        write(self.root, "mod.md", node("type: Concept\nbasis: model\n", "# Mod\n"))
        # stub_target: stub node (basis: external but stub)
        write(self.root, "stub.md", node(
            "type: Patent\ntags: [stub]\nbasis: external\nsources: ['patent-family:EP1']\n",
            "# Stub\n",
        ))

        # node without sources or link -> P2 warns
        write(self.root, "n1.md", node("type: Concept\nbasis: external\nverified_at: '2026-11-07'\n", "# N1\n"))
        # node with mixed basis without sources or link -> P2 warns
        write(self.root, "n2.md", node("type: Concept\nbasis: mixed\n", "# N2\n"))
        # node linking to external non-stub -> P2 satisfied
        write(self.root, "n3.md", node("type: Concept\nbasis: mixed\n", "# N3\n\n[see ext](ext.md)\n"))
        # node linking only to model node -> P2 warns
        write(self.root, "n4.md", node("type: Concept\nbasis: mixed\n", "# N4\n\n[see mod](mod.md)\n"))
        # node linking only to stub -> P2 warns
        write(self.root, "n5.md", node("type: Concept\nbasis: mixed\n", "# N5\n\n[see stub](stub.md)\n"))

        report = validate_wiki(self.root)
        self.assertTrue(any("n1.md: [P2]" in w for w in report.warnings))
        self.assertTrue(any("n2.md: [P2]" in w for w in report.warnings))
        self.assertFalse(any("n3.md: [P2]" in w for w in report.warnings))
        self.assertTrue(any("n4.md: [P2]" in w for w in report.warnings))
        self.assertTrue(any("n5.md: [P2]" in w for w in report.warnings))

    def test_provenance_p3_external_requires_verified_at_except_stub(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node("type: Concept\nbasis: external\nsources: ['patent-biblio:EP1']\n", "# A\n"))
        write(self.root, "stub.md", node(
            "type: Patent\ntags: [stub]\nbasis: external\nsources: ['patent-family:EP1']\n",
            "# Stub\n",
        ))
        report = validate_wiki(self.root)
        self.assertTrue(any("a.md: [P3] basis=external without verified_at" in w for w in report.warnings))
        self.assertFalse(any("stub.md: [P3]" in w for w in report.warnings))

    def test_provenance_p4_block_local_numeric_checks(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "ext.md", node(
            "type: Patent\nbasis: external\nsources: ['patent-biblio:EP1']\nverified_at: '2026-11-07'\n",
            "# Ext\n",
        ))

        # note 1: lead paragraph has raw unit number without source, but bottom has external link
        # -> P4 warns on lead paragraph line!
        n1_body = (
            "# Note 1\n\n"
            "熱伝導率は 180 W/m·K である。\n\n"
            "## Detail\n\n"
            "詳細は [Ext](ext.md) を参照。\n"
        )
        write(self.root, "n1.md", node("type: Concept\nbasis: mixed\nsources: ['patent-biblio:EP1']\n", n1_body))

        # note 2: same block has external link or external URL or sources ID -> P4 satisfied
        n2_body = (
            "# Note 2\n\n"
            "熱伝導率は 180 W/m·K である [Ext](ext.md)。\n\n"
            "比熱は 0.8 J/g·K である [web](https://example.com/spec).\n\n"
            "線膨張係数は 4.5 ppm/K である (EP1 による)。\n"
        )
        write(self.root, "n2.md", node("type: Concept\nbasis: mixed\nsources: ['patent-biblio:EP1']\n", n2_body))

        # note 3: alert and heading unverified marker scopes
        n3_body = (
            "# Note 3\n\n"
            "> [!NOTE] 一般知識（未検証）\n"
            "> 熱伝導率は 180 W/m·K である。\n"
            "> 線膨張係数は 4.5 ppm/K である。\n\n"
            "### 一般知識（未検証）\n"
            "密度は約 3.26 g/cm3 である。\n\n"
            "## 実装\n"
            "裸の数値 200 W/m·K は発火する。\n"
        )
        write(self.root, "n3.md", node("type: Concept\nbasis: mixed\nsources: ['patent-biblio:EP1']\n", n3_body))

        # note 4: table checks
        n4_body = (
            "# Note 4\n\n"
            "| 材料 | 熱伝導率（一般知識・未検証） | 破壊靭性 |\n"
            "|---|---|---|\n"
            "| AlN | 180 W/m·K | 3.0 MPa·m1/2 |\n"
            "| Si3N4 | 90 W/m·K | 6.5 MPa·m1/2 [Ext](ext.md) |\n"
        )
        write(self.root, "n4.md", node("type: Concept\nbasis: mixed\nsources: ['patent-biblio:EP1']\n", n4_body))

        # note 5: basis: external with sources non-empty is exempt from P4
        n5_body = "# Note 5\n\n熱伝導率は 180 W/m·K である。\n"
        write(self.root, "n5.md", node(
            "type: Concept\nbasis: external\nsources: ['patent-biblio:EP1']\nverified_at: '2026-11-07'\n",
            n5_body,
        ))

        # note 6: basis: model is exempt from P4
        write(self.root, "n6.md", node("type: Concept\nbasis: model\n", n5_body))

        # note 7: false positive guards (patent number, date, version, inline code, fenced code)
        n7_body = (
            "# Note 7\n\n"
            "公報番号 EP9999999B1 および 2026-11-07 公開。\n"
            "バージョン v0.2 に対応。\n"
            "インラインコード `150 W/m·K` やコードブロック:\n"
            "```\n180 W/m·K\n```\n"
        )
        write(self.root, "n7.md", node("type: Concept\nbasis: mixed\nsources: ['patent-biblio:EP1']\n", n7_body))

        report = validate_wiki(self.root)
        # n1 line 8 in file (3 in body) should warn P4
        self.assertTrue(any("n1.md:8: [P4]" in w for w in report.warnings), report.warnings)
        # n2 should have no P4
        self.assertFalse(any("n2.md" in w and "[P4]" in w for w in report.warnings))
        # n3 unverified scopes: line in "## 実装" (line 16 in file) should warn, lines inside alert/heading should not
        self.assertTrue(any("n3.md:16: [P4]" in w for w in report.warnings), report.warnings)
        self.assertFalse(any("n3.md:9: [P4]" in w for w in report.warnings))
        self.assertFalse(any("n3.md:13: [P4]" in w for w in report.warnings))
        # n4 table: row 1 (line 10 in file) lacks backing for 破壊靭性 -> warns
        # row 2 (line 11 in file) has [Ext](ext.md) -> no warning
        self.assertTrue(any("n4.md:10: [P4]" in w for w in report.warnings), report.warnings)
        self.assertFalse(any("n4.md:11: [P4]" in w for w in report.warnings))
        # n5, n6, n7 should have no P4
        self.assertFalse(any("n5.md" in w and "[P4]" in w for w in report.warnings))
        self.assertFalse(any("n6.md" in w and "[P4]" in w for w in report.warnings))
        self.assertFalse(any("n7.md" in w and "[P4]" in w for w in report.warnings))

    def test_provenance_p4_table_without_trailing_pipes(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "ext.md", node(
            "type: Patent\nbasis: external\nsources: ['patent-biblio:EP1']\nverified_at: '2026-11-07'\n",
            "# Ext\n",
        ))
        body = (
            "# Note\n\n"
            "| 材料 | 熱伝導率（一般知識・未検証） | 破壊靭性\n"
            "|---|---|---\n"
            "| AlN | 180 W/m·K | 3.0 MPa·m1/2\n"
            "| Si3N4 | 90 W/m·K | 6.5 MPa·m1/2 [Ext](ext.md)\n"
        )
        write(self.root, "t.md", node("type: Concept\nbasis: mixed\nsources: ['patent-biblio:EP1']\n", body))
        report = validate_wiki(self.root)
        # Row 1 (Line 10): 破壊靭性 lacks backing -> warns
        self.assertTrue(any("t.md:10: [P4]" in w for w in report.warnings), report.warnings)
        # Row 2 (Line 11): 破壊靭性 has [Ext](ext.md) -> no warning
        self.assertFalse(any("t.md:11: [P4]" in w for w in report.warnings))

    def test_provenance_p5_external_with_unverified_marker_warns(self):
        write(self.root, "index.md", "# Index\n")
        body_alert = "# Ext\n\n> [!NOTE] 一般知識（未検証）\n> note\n"
        write(self.root, "a1.md", node(
            "type: Concept\nbasis: external\nsources: ['patent-biblio:EP1']\nverified_at: '2026-11-07'\n",
            body_alert,
        ))
        body_heading = "# Ext\n\n### General Knowledge (Unverified)\nsome text\n"
        write(self.root, "a2.md", node(
            "type: Concept\nbasis: external\nsources: ['patent-biblio:EP1']\nverified_at: '2026-11-07'\n",
            body_heading,
        ))
        report = validate_wiki(self.root)
        self.assertTrue(any("a1.md: [P5]" in w for w in report.warnings))
        self.assertTrue(any("a2.md: [P5]" in w for w in report.warnings))

    def test_provenance_p6_web_sources_require_retrieved(self):
        write(self.root, "index.md", "# Index\n")
        # missing retrieved on web-type sources
        for i, kind in enumerate(["product-page", "press-release", "news", "web"]):
            write(self.root, f"w{i}.md", node(
                f"type: Concept\nbasis: external\nsources: ['{kind}:https://example.com/{i}']\n"
                "verified_at: '2026-11-07'\n",
                "# W\n",
            ))
        # paper does not require retrieved
        write(self.root, "paper.md", node(
            "type: Concept\nbasis: external\nsources: ['paper:10.1234/5678 issued=2024-01-01']\n"
            "verified_at: '2026-11-07'\n",
            "# Paper\n",
        ))
        report = validate_wiki(self.root)
        for i in range(4):
            self.assertTrue(any(f"w{i}.md: [P6]" in w for w in report.warnings))
        self.assertFalse(any("paper.md: [P6]" in w for w in report.warnings))

    def test_provenance_p7_unknown_source_kind_warns_without_failing(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node(
            "type: Concept\nbasis: external\nsources: ['blog:https://example.com/post retrieved=2026-11-07']\n"
            "verified_at: '2026-11-07'\n",
            "# A\n",
        ))
        report = validate_wiki(self.root)
        self.assertTrue(report.ok)  # exit 0
        self.assertTrue(any("a.md: [P7] unknown source kind: blog" in w for w in report.warnings))

    def test_provenance_p8_volatile_sources_age_check_with_injected_today(self):
        write(self.root, "index.md", "# Index\n")
        # volatile source >= 3 years (1095 days) old
        # 2026-11-07 - 1095 days = 2023-11-08. Date 2023-11-01 is >= 1095 days old.
        write(self.root, "old_news.md", node(
            "type: Concept\nbasis: mixed\nsources: ['news:https://news.example.com retrieved=2023-11-01']\n",
            "# Old\n",
        ))
        # fresh news
        write(self.root, "fresh_news.md", node(
            "type: Concept\nbasis: mixed\nsources: ['news:https://news.example.com retrieved=2026-01-01']\n",
            "# Fresh\n",
        ))
        # mixed with paper: exempt from P8 (only all volatile triggers P8)
        write(self.root, "mixed_kinds.md", node(
            "type: Concept\nbasis: mixed\nsources: ['news:https://news.ex retrieved=2023-11-01', 'paper:10.1234/1']\n",
            "# Mixed\n",
        ))
        # old news but fresh verified_at: exempt
        write(self.root, "verified_news.md", node(
            "type: Concept\nbasis: mixed\nsources: ['news:https://news.ex retrieved=2023-11-01']\n"
            "verified_at: '2026-11-01'\n",
            "# Verified\n",
        ))

        report = validate_wiki(self.root, today=date(2026, 11, 7))
        self.assertTrue(any("old_news.md: [P8]" in w for w in report.warnings))
        self.assertFalse(any("fresh_news.md: [P8]" in w for w in report.warnings))
        self.assertFalse(any("mixed_kinds.md: [P8]" in w for w in report.warnings))
        self.assertFalse(any("verified_news.md: [P8]" in w for w in report.warnings))

    def test_strict_provenance_promotes_p2_p4_p5_to_errors(self):
        write(self.root, "index.md", "# Index\n")
        # P2: basis external without sources
        write(self.root, "p2.md", node("type: Concept\nbasis: external\nverified_at: '2026-11-07'\n", "# P2\n"))
        # P4: unverified numeric value in mixed node
        write(self.root, "p4.md", node(
            "type: Concept\nbasis: mixed\nsources: ['patent-biblio:EP1']\n",
            "# P4\n\n熱伝導率は 180 W/m·K である。\n",
        ))
        # P5: basis external with unverified marker
        write(self.root, "p5.md", node(
            "type: Concept\nbasis: external\nsources: ['patent-biblio:EP1']\nverified_at: '2026-11-07'\n",
            "# P5\n\n> [!NOTE] 一般知識（未検証）\n> note\n",
        ))
        # P1, P3, P6, P7 (should stay in warnings)
        write(self.root, "p1.md", node("type: Concept\n", "# P1\n"))
        write(self.root, "p3.md", node(
            "type: Concept\nbasis: external\nsources: ['patent-biblio:EP1']\n",
            "# P3\n",
        ))
        write(self.root, "p6.md", node(
            "type: Concept\nbasis: external\nsources: ['web:https://ex.com']\nverified_at: '2026-11-07'\n",
            "# P6\n",
        ))
        write(self.root, "p7.md", node(
            "type: Concept\nbasis: external\nsources: ['blog:https://ex.com']\nverified_at: '2026-11-07'\n",
            "# P7\n",
        ))

        # without strict_provenance
        normal_report = validate_wiki(self.root)
        self.assertTrue(normal_report.ok)  # exit 0
        self.assertTrue(any("p2.md: [P2]" in w for w in normal_report.warnings))
        self.assertTrue(any("p4.md:" in w and "[P4]" in w for w in normal_report.warnings))
        self.assertTrue(any("p5.md: [P5]" in w for w in normal_report.warnings))

        # with strict_provenance
        strict_report = validate_wiki(self.root, strict_provenance=True)
        self.assertFalse(strict_report.ok)  # exit 1
        self.assertTrue(any("p2.md: [P2]" in e for e in strict_report.errors))
        self.assertTrue(any("p4.md:" in e and "[P4]" in e for e in strict_report.errors))
        self.assertTrue(any("p5.md: [P5]" in e for e in strict_report.errors))
        # P1, P3, P6, P7 should remain in warnings
        self.assertTrue(any("p1.md: [P1]" in w for w in strict_report.warnings))
        self.assertTrue(any("p3.md: [P3]" in w for w in strict_report.warnings))
        self.assertTrue(any("p6.md: [P6]" in w for w in strict_report.warnings))
        self.assertTrue(any("p7.md: [P7]" in w for w in strict_report.warnings))

    def test_ingested_patent_and_stub_have_zero_provenance_warnings(self):
        class SimpleFakeEpo:
            def biblio(self, pub):
                return {
                    "title": "Alloy",
                    "applicants": ["CORP"],
                    "country": "EP",
                    "number": "9999999",
                    "kind": "B1",
                    "publication_date": "20180131",
                }
            def family(self, pub):
                return [PublicationRef("US", "1234567", "B2", "20190205", "1")]
            def claims(self, pub):
                return ["1. An amorphous alloy."]

        ingest_publication(SimpleFakeEpo(), "EP9999999B1", self.root, now="2026-10-29T00:00:00Z")
        report = validate_wiki(self.root)
        provenance_warnings = [w for w in report.warnings if any(f"[P{i}]" in w for i in range(1, 9))]
        self.assertEqual(provenance_warnings, [])
        self.assertTrue(report.ok)

    def test_concept_skeleton_in_docs_and_skill_validates_cleanly(self):
        docs_path = Path(__file__).resolve().parent.parent / "docs" / "okf_wiki.md"
        skill_path = Path(__file__).resolve().parent.parent / ".agents" / "skills" / "okf-wiki" / "SKILL.md"

        self.assertTrue(docs_path.exists(), f"missing {docs_path}")
        self.assertTrue(skill_path.exists(), f"missing {skill_path}")

        docs_text = docs_path.read_text(encoding="utf-8")
        skill_text = skill_path.read_text(encoding="utf-8")

        # SKILL.md line limit: <= 250 lines
        self.assertLessEqual(
            len(skill_text.splitlines()), 250,
            f"SKILL.md exceeds 250 lines ({len(skill_text.splitlines())} lines)"
        )

        skeleton_re = re.compile(
            r"<!--\s*okf-concept-skeleton:start\s*-->\s*```(?:markdown)?\n(.*?)\n```\s*<!--\s*okf-concept-skeleton:end\s*-->",
            re.DOTALL,
        )
        docs_match = skeleton_re.search(docs_text)
        skill_match = skeleton_re.search(skill_text)

        self.assertIsNotNone(docs_match, "docs/okf_wiki.md missing concept skeleton marker")
        self.assertIsNotNone(skill_match, "SKILL.md missing concept skeleton marker")

        docs_skeleton = docs_match.group(1).strip()
        skill_skeleton = skill_match.group(1).strip()
        self.assertEqual(docs_skeleton, skill_skeleton, "skeleton in docs and SKILL.md must match")

        # Validate the extracted skeleton in a clean wiki
        write(self.root, "index.md", "# Index\n")
        write(self.root, "patents/EP9999999B1.md", node(
            "type: Patent\n"
            "title: 'AlN Patent'\n"
            "basis: external\n"
            "sources: ['patent-biblio:EP9999999B1 via=epo-ops']\n"
            "verified_at: '2026-11-07'\n",
            "# AlN Patent\n",
        ))
        write(self.root, "concepts/aln.md", docs_skeleton + "\n")

        report = validate_wiki(self.root, strict_provenance=True)
        self.assertEqual(report.errors, [], f"skeleton validation errors: {report.errors}")
        self.assertEqual(report.warnings, [], f"skeleton validation warnings: {report.warnings}")
        self.assertTrue(report.ok)


if __name__ == "__main__":
    unittest.main()
