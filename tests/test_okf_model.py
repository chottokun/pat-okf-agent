"""Tests for OKF frontmatter handling and concept nodes (no network, no deps)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.frontmatter import (
    FrontmatterError,
    dump_document,
    dump_frontmatter,
    parse_frontmatter,
)
from pat_okf_agent.wiki.model import (
    SCOPE_WEIGHTS,
    Concept,
    extract_links,
    extract_sections,
    iter_concepts,
    load_concept,
    relevance,
    scope_from_heading,
    scope_of,
    tokenize,
    validate_meta,
    write_concept,
)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def node(meta: str, body: str = "# Note\n") -> str:
    return f"---\n{meta}---\n{body}"


class FrontmatterTests(unittest.TestCase):
    def test_parse_scalars_lists_and_types(self):
        meta, body = parse_frontmatter(
            "---\n"
            'type: Patent\n'
            'title: "A: B"\n'
            "tags:\n"
            "  - epo\n"
            "  - patent\n"
            "count: 3\n"
            "ratio: 0.5\n"
            "active: true\n"
            "missing: null\n"
            "inline: [a, \"b c\"]\n"
            "---\n"
            "# Hi\n"
        )
        self.assertEqual(meta["type"], "Patent")
        self.assertEqual(meta["title"], "A: B")
        self.assertEqual(meta["tags"], ["epo", "patent"])
        self.assertEqual(meta["count"], 3)
        self.assertEqual(meta["ratio"], 0.5)
        self.assertIs(meta["active"], True)
        self.assertIsNone(meta["missing"])
        self.assertEqual(meta["inline"], ["a", "b c"])
        self.assertEqual(body, "# Hi\n")

    def test_trailing_block_list_is_not_overwritten(self):
        meta, body = parse_frontmatter(
            "---\n"
            "name: Demo\n"
            "trigger_conditions:\n"
            "  - patent\n"
            "  - 特許\n"
            "---\n"
            "# Body\n"
        )
        self.assertEqual(meta["trigger_conditions"], ["patent", "特許"])
        self.assertEqual(body, "# Body\n")

    def test_trailing_empty_key_becomes_none(self):
        meta, _ = parse_frontmatter("---\ntype: Note\nmissing:\n---\n")
        self.assertIsNone(meta["missing"])

    def test_roundtrip_quotes_colons_hashes_and_newlines(self):
        meta = {
            "type": "Note",
            "title": 'A: B # not a comment',
            "tags": ["a b", "c"],
            "empty": "",
            "text": "line1\nline2",
            "n": 7,
            "flag": False,
            "none": None,
        }
        text = dump_document(meta, "Body\n")
        parsed, body = parse_frontmatter(text)
        self.assertEqual(parsed, meta)
        self.assertEqual(body, "Body\n")

    def test_dump_frontmatter_is_deterministic(self):
        text = dump_frontmatter({"tags": ["b", "a"], "type": "Note",
                                 "title": "T"})
        self.assertEqual(
            text,
            "---\ntype: Note\ntitle: T\ntags: [b, a]\n---\n",
        )

    def test_missing_or_unclosed_frontmatter_raises(self):
        for text in ("# no frontmatter\n", "---\ntype: X\n# unclosed\n"):
            with self.subTest(text=text):
                with self.assertRaises(FrontmatterError):
                    parse_frontmatter(text)

    def test_nested_mapping_is_rejected(self):
        with self.assertRaises(FrontmatterError):
            parse_frontmatter("---\ntype: X\nmeta:\n  nested: y\n---\n")

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(FrontmatterError):
            parse_frontmatter("---\ntype: X\ntype: Y\n---\n")

    def test_inline_comment_is_stripped_but_hash_in_quotes_is_kept(self):
        meta, _ = parse_frontmatter(
            "---\ntype: X  # comment\ntitle: \"a # b\"\n---\n")
        self.assertEqual(meta["type"], "X")
        self.assertEqual(meta["title"], "a # b")


class ConceptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_load_concept_extracts_meta_links_and_fallbacks(self):
        write(self.root, "patents/EP1.md", node(
            "type: Patent\ntitle: Example\ndescription: A sample\n"
            "tags: [EPO]\ntimestamp: 2026-10-29T00:00:00Z\n",
            "# Example\n\nSee [family](../family/EP2.md) and "
            "[web](https://example.com) and [here](#x).\n",
        ))
        concept = load_concept(self.root / "patents/EP1.md", self.root)
        self.assertEqual(concept.rel, "patents/EP1.md")
        self.assertEqual(concept.type, "Patent")
        self.assertEqual(concept.title, "Example")
        self.assertEqual(concept.description, "A sample")
        self.assertEqual(concept.tags, ["EPO"])
        self.assertEqual([link.target for link in concept.links],
                         ["../family/EP2.md"])
        self.assertEqual(concept.links[0].resolved, "family/EP2.md")

    def test_title_falls_back_to_heading_then_stem(self):
        write(self.root, "a.md", node("type: Note\n", "# Heading A\n"))
        write(self.root, "b.md", node("type: Note\n", "plain\n"))
        self.assertEqual(load_concept(self.root / "a.md", self.root).title,
                         "Heading A")
        self.assertEqual(load_concept(self.root / "b.md", self.root).title,
                         "b")

    def test_links_in_code_are_ignored(self):
        write(self.root, "a.md", node(
            "type: Note\n",
            "`[inline](x.md)`\n\n```\n[fenced](y.md)\n```\n\n"
            "[real](z.md)\n",
        ))
        concept = load_concept(self.root / "a.md", self.root)
        self.assertEqual([link.target for link in concept.links], ["z.md"])

    def test_link_escaping_root_is_not_resolved(self):
        write(self.root, "a.md", node("type: Note\n", "[out](../../x.md)\n"))
        link = load_concept(self.root / "a.md", self.root).links[0]
        self.assertFalse(link.external)
        self.assertIsNone(link.resolved)

    def test_scope_from_standard_headings(self):
        cases = {
            "Taxonomy": "taxonomy",
            "Workflow / Implementation": "workflow",
            "Implementation notes": "workflow",
            "Synergistic Combinations": "synergistic",
            "Gotchas / Anti-Pitfalls": "gotchas",
            "失敗事例": "gotchas",
            "関連": None,
        }
        for heading, expected in cases.items():
            with self.subTest(heading=heading):
                self.assertEqual(scope_from_heading(heading), expected)

    def test_links_capture_nearest_heading_scope_and_context(self):
        body = (
            "# A\n"
            "## Taxonomy\n"
            "[alloy](b.md)\n"
            "## Workflow / Implementation\n"
            "[steps](c.md)\n"
            "### Detail\n"
            "[deep](d.md)\n"
            "## Gotchas / Anti-Pitfalls\n"
            "[failure](e.md)\n"
            "## Related\n"
            "[other](f.md)\n"
        )
        links = extract_links(body, "a.md")
        self.assertEqual([link.section_scope for link in links],
                         ["taxonomy", "workflow", "workflow", "gotchas", None])
        self.assertEqual([link.heading for link in links],
                         ["Taxonomy", "Workflow / Implementation", "Detail",
                          "Gotchas / Anti-Pitfalls", "Related"])
        self.assertIn("alloy", links[0].context)
        self.assertIn("Taxonomy", links[0].context)

    def test_extract_sections_is_fence_aware_and_inherits_scope(self):
        body = (
            "intro\n"
            "# A\n"
            "text\n"
            "## Taxonomy\n"
            "definition\n"
            "### Detail\n"
            "leaf\n"
            "## Gotchas / Anti-Pitfalls\n"
            "- INCOMPATIBLE_EXPLICIT\n"
            "```\n## Not a heading\n```\n"
        )
        sections = extract_sections(body)
        self.assertEqual(sections[0].heading, "")
        self.assertEqual([section.heading for section in sections],
                         ["", "A", "Taxonomy", "Detail",
                          "Gotchas / Anti-Pitfalls"])
        self.assertIsNone(sections[0].scope)
        self.assertEqual(sections[2].scope, "taxonomy")
        self.assertEqual(sections[3].scope, "taxonomy")
        self.assertEqual(sections[4].scope, "gotchas")
        self.assertIn("INCOMPATIBLE_EXPLICIT", sections[4].text)
        self.assertIn("## Not a heading", sections[4].text)

    def test_tokenize_and_relevance(self):
        self.assertEqual(tokenize("Amorphous_ALLoy アモルファス"),
                         ["amorphous_alloy", "アモルファス"])
        self.assertAlmostEqual(relevance(set(tokenize("amorphous")),
                                         "Amorphous alloy"), 1.0)
        self.assertAlmostEqual(relevance({"amorphous", "alloy"},
                                         "amorphous"), 0.5)
        self.assertEqual(relevance(set(), "anything"), 1.0)

    def test_scope_inference(self):
        self.assertEqual(scope_of({"type": "Gotcha"}), "gotchas")
        self.assertEqual(scope_of({"type": "Note", "tags": ["workflow"]}),
                         "workflow")
        self.assertEqual(scope_of({"type": "Note", "scope": "Synergistic"}),
                         "synergistic")
        self.assertEqual(scope_of({"type": "Note"}), "taxonomy")
        self.assertEqual(set(SCOPE_WEIGHTS),
                         {"taxonomy", "workflow", "synergistic", "gotchas"})
        self.assertEqual(SCOPE_WEIGHTS["gotchas"], 2.0)

    def test_validate_meta(self):
        self.assertEqual(validate_meta({"type": "Note"}), [])
        self.assertTrue(validate_meta({}))
        self.assertTrue(validate_meta({"type": ""}))
        self.assertTrue(validate_meta({"type": "Note", "tags": "EPO"}))
        self.assertTrue(validate_meta(
            {"type": "Note", "timestamp": "not-a-date"}))
        self.assertTrue(validate_meta({"type": "Note", "scope": "unknown"}))

    def test_iter_concepts_excludes_navigation_and_sorts(self):
        write(self.root, "b.md", node("type: Note\n"))
        write(self.root, "a/z.md", node("type: Note\n"))
        write(self.root, "index.md", node("type: Index\n"))
        write(self.root, "sub/log.md", node("type: Log\n"))
        write(self.root, "notes.txt", "ignore")
        rels = [c.rel for c in iter_concepts(self.root)]
        self.assertEqual(rels, ["a/z.md", "b.md"])

    def test_write_concept_is_atomic_and_roundtrips(self):
        target = self.root / "x/y.md"
        write_concept(target, {"type": "Note", "title": "Y"}, "# Y\n")
        self.assertEqual(load_concept(target, self.root).title, "Y")
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_concept_is_a_dataclass(self):
        self.assertIsInstance(
            Concept(path=Path("a.md"), rel="a.md", meta={"type": "Note"},
                    body="", links=[]),
            Concept,
        )


if __name__ == "__main__":
    unittest.main()
