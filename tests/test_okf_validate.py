"""Tests for OKF validation (structure, links, orphans, generated indexes)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.validate import validate_wiki


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


if __name__ == "__main__":
    unittest.main()
