"""Tests for okf claims deterministic claim extraction, CLI, and kb-audit skill."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from okf_kb_agent.wiki.claims import extract_claims
from okf_kb_agent.wiki.cli import discover_skills, main as cli_main
from okf_kb_agent.wiki.tooling import TOOL_SPECS


def write_note(root: Path, rel: str, meta: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


class ClaimsCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "concepts").mkdir(parents=True, exist_ok=True)

    def test_extracts_numeric_sentences_and_table_rows_with_sources_and_sections(self):
        """(1) Numeric sentences & table rows extracted; (2) no-number & fences excluded; (3) sources attached."""
        write_note(
            self.root,
            "concepts/vapor-chamber.md",
            (
                "type: Concept\n"
                "title: 'Vapor Chamber'\n"
                "sources:\n"
                "  - paper:10.1000/vc-2024\n"
                "  - datasheet:https://example.com/vc.pdf\n"
            ),
            (
                "Lead sentence before any heading has 2 phases.\n"
                "\n"
                "## Thermal Performance\n"
                "\n"
                "Purely qualitative sentence without numbers should be skipped.\n"
                "Effective conductivity reaches 5000 W/mK at 10–20°C and 95.5% efficiency.\n"
                "Decay constant is 1.5e-3 per second.\n"
                "\n"
                "| Metric | Value |\n"
                "| :--- | :--- |\n"
                "| Thickness | 0.4 mm |\n"
                "| Qualitative Note | High reliability |\n"
                "\n"
                "```python\n"
                "ignored_value = 9999\n"
                "```\n"
                "\n"
                "~~~\n"
                "also_ignored = 42%\n"
                "~~~\n"
            ),
        )
        # concepts/index.md must be ignored
        write_note(
            self.root,
            "concepts/index.md",
            "type: Index\ntitle: 'Concepts'\n",
            "- 100 items\n",
        )

        res = extract_claims(self.root)
        self.assertEqual(res.total_claims, 4)
        self.assertEqual(res.returned, 4)
        self.assertEqual(res.seed, 0)

        texts = [c.text for c in res.claims]
        self.assertEqual(
            texts,
            [
                "Lead sentence before any heading has 2 phases.",
                "Effective conductivity reaches 5000 W/mK at 10–20°C and 95.5% efficiency.",
                "Decay constant is 1.5e-3 per second.",
                "| Thickness | 0.4 mm |",
            ],
        )

        # Verify section tracking (None before first heading, heading name afterwards)
        self.assertIsNone(res.claims[0].section)
        self.assertEqual(res.claims[1].section, "Thermal Performance")
        self.assertEqual(res.claims[3].section, "Thermal Performance")

        # Verify 1-based line numbers (frontmatter is 7 lines, body starts at line 8)
        self.assertEqual(res.claims[0].line, 8)
        self.assertEqual(res.claims[1].line, 13)
        self.assertEqual(res.claims[2].line, 14)
        self.assertEqual(res.claims[3].line, 18)

        # Verify frontmatter sources preserved verbatim
        expected_sources = [
            "paper:10.1000/vc-2024",
            "datasheet:https://example.com/vc.pdf",
        ]
        for c in res.claims:
            self.assertEqual(c.note, "concepts/vapor-chamber.md")
            self.assertEqual(c.sources, expected_sources)
            self.assertTrue(len(c.numbers) >= 1)

        # Verify specific numeric tokens captured
        self.assertIn("10–20°C", res.claims[1].numbers)
        self.assertIn("95.5%", res.claims[1].numbers)
        self.assertIn("1.5e-3", res.claims[2].numbers)
        self.assertIn("0.4", res.claims[3].numbers)

        # Verify excluded content never appears
        dumped = json.dumps(res.to_dict(), ensure_ascii=False)
        self.assertNotIn("9999", dumped)
        self.assertNotIn("42%", dumped)
        self.assertNotIn("High reliability", dumped)
        self.assertNotIn("qualitative sentence without numbers", dumped)
        self.assertNotIn("100 items", dumped)

    def test_deterministic_sampling_by_seed(self):
        """(4) Same seed reproduces the exact same sampled claim sequence."""
        lines = "\n".join(f"Claim number {i} has value {i * 10} W." for i in range(1, 21))
        write_note(
            self.root,
            "concepts/heat-pipe.md",
            "type: Concept\ntitle: 'Heat Pipe'\nsources: [paper:10.1000/hp]\n",
            f"## Metrics\n{lines}\n",
        )

        r1 = extract_claims(self.root, sample=5, seed=42)
        r2 = extract_claims(self.root, sample=5, seed=42)
        r3 = extract_claims(self.root, sample=5, seed=99)

        self.assertEqual(r1.total_claims, 20)
        self.assertEqual(r1.returned, 5)
        self.assertEqual(r1.seed, 42)
        self.assertEqual(r1.to_dict(), r2.to_dict())
        self.assertNotEqual(
            [c.text for c in r1.claims],
            [c.text for c in r3.claims],
        )

    def test_note_filter(self):
        """(5) --note filter restricts extraction to specified concept slugs."""
        write_note(
            self.root,
            "concepts/alpha.md",
            "type: Concept\ntitle: 'Alpha'\n",
            "Alpha reaches 10 W.\n",
        )
        write_note(
            self.root,
            "concepts/beta.md",
            "type: Concept\ntitle: 'Beta'\n",
            "Beta reaches 20 W.\n",
        )
        write_note(
            self.root,
            "concepts/gamma.md",
            "type: Concept\ntitle: 'Gamma'\n",
            "Gamma reaches 30 W.\n",
        )

        res = extract_claims(self.root, notes=["alpha", "gamma"])
        self.assertEqual(res.total_claims, 2)
        self.assertEqual(
            [c.note for c in res.claims],
            ["concepts/alpha.md", "concepts/gamma.md"],
        )


class ClaimsCliAndSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "concepts").mkdir(parents=True, exist_ok=True)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_claims_json_output_and_flags(self):
        write_note(
            self.root,
            "concepts/tim.md",
            "type: Concept\ntitle: 'TIM'\nsources: [paper:10.1000/tim]\n",
            "## Properties\nConductivity is 8.5 W/mK.\nBond line is 25 um.\n",
        )
        write_note(
            self.root,
            "concepts/other.md",
            "type: Concept\ntitle: 'Other'\n",
            "Other value is 12 W.\n",
        )

        code, out, err = self.run_cli(
            "claims", str(self.root), "--note", "tim", "--sample", "1", "--seed", "7", "--json"
        )
        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(set(payload.keys()), {"wiki", "total_claims", "returned", "seed", "claims"})
        self.assertEqual(payload["total_claims"], 2)
        self.assertEqual(payload["returned"], 1)
        self.assertEqual(payload["seed"], 7)
        self.assertEqual(len(payload["claims"]), 1)
        claim = payload["claims"][0]
        self.assertEqual(
            set(claim.keys()),
            {"note", "line", "section", "text", "numbers", "sources"},
        )
        # No verdicts, scores, or weights
        for forbidden in ('"status"', '"verdict"', '"score"', '"weight"', '"severity"'):
            self.assertNotIn(forbidden, out)

        # Also works without --json (JSON is default)
        code2, out2, _ = self.run_cli(
            "claims", str(self.root), "--note", "tim", "--sample", "1", "--seed", "7"
        )
        self.assertEqual(code2, 0)
        self.assertEqual(json.loads(out2), payload)

    def test_cli_claims_errors(self):
        missing = self.root / "does-not-exist"
        code, _, err = self.run_cli("claims", str(missing))
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

        code2, _, err2 = self.run_cli("claims", str(self.root), "--sample", "-1")
        self.assertEqual(code2, 2)
        self.assertIn("error:", err2)

    def test_claims_not_in_okf_schema(self):
        """Match candidate-check: claims is not exposed in okf schema / MCP TOOL_SPECS."""
        tool_names = {spec["name"] for spec in TOOL_SPECS}
        self.assertNotIn("okf_claims", tool_names)
        self.assertNotIn("claims", tool_names)
        self.assertNotIn("okf_candidate_check", tool_names)

    def test_kb_audit_skill_discovered_and_valid(self):
        repo_root = Path(__file__).resolve().parent.parent
        specs = discover_skills(repo_root, None)
        by_name = {s["name"]: s for s in specs}
        self.assertIn("KB Audit", by_name)
        audit_spec = by_name["KB Audit"]
        self.assertNotIn("error", audit_spec)
        for trigger in ("audit", "promotion gate", "監査", "昇格ゲート"):
            self.assertIn(trigger, audit_spec["triggers"])


if __name__ == "__main__":
    unittest.main()
