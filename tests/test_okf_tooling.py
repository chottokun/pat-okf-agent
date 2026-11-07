"""Tests for the stable JSON tool contracts and dispatcher."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.tooling import (
    TOOL_SPECS,
    ToolError,
    dispatch_tool,
    wiki_stats,
)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def node(meta: str, body: str = "# Note\n") -> str:
    return f"---\n{meta}---\n{body}"


class ToolingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.wiki = self.base / "wiki"
        write(self.wiki, "index.md", "# Index\n")
        write(self.wiki, "a.md", node(
            "type: Note\ntitle: Amorphous alloy\n",
            "# Amorphous alloy\nSee [b](b.md).\n"))
        write(self.wiki, "b.md", node(
            "type: Workflow\ntitle: Casting\n", "# Casting\n"))

    def test_specs_are_unique_and_have_required_schema_fields(self):
        names = [spec["name"] for spec in TOOL_SPECS]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("okf_context", names)
        for spec in TOOL_SPECS:
            with self.subTest(name=spec["name"]):
                self.assertTrue(spec["description"])
                schema = spec["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIn("wiki", schema["required"])
                if spec["name"] in ("okf_search", "okf_context"):
                    self.assertIn("query", schema["required"])
                self.assertFalse(schema["additionalProperties"])

    def test_search_and_stats(self):
        hits = dispatch_tool(
            "okf_search", {"wiki": "wiki", "query": "amorphous"},
            root=self.base)
        self.assertEqual(hits[0]["node"], "a.md")
        stats = dispatch_tool("okf_stats", {"wiki": "wiki"}, root=self.base)
        self.assertEqual(stats["concept_count"], 2)
        self.assertEqual(stats["links"], 1)
        self.assertEqual(stats["pattern"], "A")
        self.assertEqual(stats["basis"], {"external": 0, "model": 0, "mixed": 0, "undeclared": 2})

    def test_wiki_stats_counts_all_four_basis_buckets(self):
        write(self.base / "wiki2", "index.md", "# Index\n")
        write(self.base / "wiki2", "ext.md", node("type: Patent\nbasis: external\n"))
        write(self.base / "wiki2", "mod.md", node("type: Concept\nbasis: model\n"))
        write(self.base / "wiki2", "mix.md", node("type: Concept\nbasis: mixed\n"))
        write(self.base / "wiki2", "und.md", node("type: Note\n"))
        stats = dispatch_tool("okf_stats", {"wiki": "wiki2"}, root=self.base)
        self.assertEqual(stats["basis"], {
            "external": 1,
            "model": 1,
            "mixed": 1,
            "undeclared": 1,
        })

    def test_context_traverse_and_validate(self):
        bundle = dispatch_tool(
            "okf_context",
            {"wiki": "wiki", "query": "amorphous", "max_nodes": 3},
            root=self.base,
        )
        self.assertIn("nodes", bundle)
        hits = dispatch_tool(
            "okf_traverse", {"wiki": "wiki", "seed": "a"},
            root=self.base,
        )
        self.assertEqual(hits[0]["node"], "b.md")
        report = dispatch_tool(
            "okf_validate", {"wiki": "wiki"}, root=self.base)
        self.assertTrue(report["ok"])

    def test_index_check_then_write(self):
        before = dispatch_tool(
            "okf_index", {"wiki": "wiki", "check": True}, root=self.base)
        self.assertFalse(before["ok"])
        self.assertIn("index.md", before["stale"])
        written = dispatch_tool(
            "okf_index", {"wiki": "wiki"}, root=self.base)
        self.assertTrue(written["ok"])
        after = dispatch_tool(
            "okf_index", {"wiki": "wiki", "check": True}, root=self.base)
        self.assertTrue(after["ok"])

    def test_root_escape_is_rejected(self):
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_stats", {"wiki": "../outside"}, root=self.base)
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_stats", {"wiki": "/"}, root=self.base)

    def test_auxiliary_path_escape_is_rejected(self):
        # db_path escapes root
        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                "okf_search",
                {"wiki": "wiki", "query": "amorphous", "backend": "sqlite",
                 "db_path": "../outside.sqlite3"},
                root=self.base)
        self.assertIn("db_path escapes the allowed root", str(ctx.exception))
        self.assertFalse((self.base.parent / "outside.sqlite3").exists())

        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                "okf_context",
                {"wiki": "wiki", "query": "amorphous", "backend": "sqlite",
                 "db_path": "/tmp/escape.sqlite3"},
                root=self.base)
        self.assertIn("db_path escapes the allowed root", str(ctx.exception))

        # db_path is a directory
        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                "okf_search",
                {"wiki": "wiki", "query": "amorphous", "backend": "sqlite",
                 "db_path": "wiki"},
                root=self.base)
        self.assertIn("db_path must not be a directory", str(ctx.exception))

        # cache_dir escapes root
        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                "okf_ingest_epo",
                {"wiki": "wiki", "publication": "EP1000000A1",
                 "cache_dir": "../outside_cache"},
                root=self.base)
        self.assertIn("cache_dir escapes the allowed root", str(ctx.exception))

        # credentials escapes root
        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                "okf_ingest_epo",
                {"wiki": "wiki", "publication": "EP1000000A1",
                 "credentials": "../outside_creds.json"},
                root=self.base)
        self.assertIn("credentials escapes the allowed root", str(ctx.exception))

        # credentials file does not exist
        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                "okf_ingest_epo",
                {"wiki": "wiki", "publication": "EP1000000A1",
                 "credentials": "missing_creds.json"},
                root=self.base)
        self.assertIn("credentials file does not exist", str(ctx.exception))

        # cache_dir must be a directory
        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                "okf_ingest_epo",
                {"wiki": "wiki", "publication": "EP1000000A1",
                 "cache_dir": "wiki/a.md"},
                root=self.base)
        self.assertIn("cache_dir must be a directory", str(ctx.exception))

        # empty or whitespace path strings
        for key in ("db_path",):
            with self.assertRaises(ToolError) as ctx:
                dispatch_tool(
                    "okf_search",
                    {"wiki": "wiki", "query": "amorphous", key: "  "},
                    root=self.base)
            self.assertIn(f"{key} must be a non-empty path", str(ctx.exception))

        # valid confined db_path works
        custom_db = "wiki/.okf/custom.sqlite3"
        hits = dispatch_tool(
            "okf_search",
            {"wiki": "wiki", "query": "amorphous", "backend": "sqlite",
             "db_path": custom_db},
            root=self.base)
        self.assertTrue(hits)
        self.assertTrue((self.base / custom_db).exists())

    def test_bad_arguments_are_rejected(self):
        with self.assertRaises(ToolError):
            dispatch_tool("okf_nope", {"wiki": "wiki"}, root=self.base)
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_search", {"wiki": "wiki", "query": "x", "limit": 0},
                root=self.base)
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_traverse", {"wiki": "wiki", "seed": "missing"},
                root=self.base)
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_ingest_epo", {"wiki": "wiki"}, root=self.base)
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_ingest_epo",
                {"wiki": "wiki", "publication": "EP1", "query": "x"},
                root=self.base,
            )
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_index", {"wiki": "wiki", "check": "false"},
                root=self.base,
            )
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_traverse",
                {"wiki": "wiki", "seed": "a", "scopes": ["bogus"]},
                root=self.base,
            )


    def test_synonym_expansion_and_qdrant_argument_validation(self):
        write(self.wiki, "synonyms.json", json.dumps([
            ["amorphous alloy", "metallic glass"],
        ]))
        baseline = dispatch_tool(
            "okf_search",
            {"wiki": "wiki", "query": "metallic glass"},
            root=self.base,
        )
        self.assertEqual(baseline, [])
        expanded = dispatch_tool(
            "okf_search",
            {"wiki": "wiki", "query": "metallic glass",
             "expand_synonyms": True},
            root=self.base,
        )
        self.assertEqual(expanded[0]["node"], "a.md")
        with self.assertRaises(ToolError):
            dispatch_tool(
                "okf_search",
                {"wiki": "wiki", "query": "x", "backend": "qdrant"},
                root=self.base,
            )
        schema = next(spec for spec in TOOL_SPECS
                      if spec["name"] == "okf_search")["inputSchema"]
        self.assertIn("qdrant", schema["properties"]["backend"]["enum"])
        self.assertIn("expand_synonyms", schema["properties"])

    def test_wiki_stats_matches_dispatcher(self):
        direct = wiki_stats(self.wiki)
        via_tool = dispatch_tool(
            "okf_stats", {"wiki": "wiki"}, root=self.base)
        self.assertEqual(direct, via_tool)


if __name__ == "__main__":
    unittest.main()
