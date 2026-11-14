"""Tests for search hit link counts (information-only fields)."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from okf_kb_agent.wiki.cli import main
from okf_kb_agent.wiki.search import (
    LocalSearchBackend,
    SearchHit,
    attach_link_counts,
)
from okf_kb_agent.wiki.tooling import dispatch_tool


def write_doc(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


class SearchHitLinkCountsTests(unittest.TestCase):
    def test_search_hit_as_dict_omits_none_and_includes_when_set(self):
        hit_none = SearchHit(
            node="concepts/test.md",
            score=10.0,
            title="Test",
            snippet="A snippet",
            matched_terms=1,
        )
        d_none = hit_none.as_dict()
        self.assertNotIn("outgoing_links", d_none)
        self.assertNotIn("backlinks", d_none)

        hit_set = SearchHit(
            node="concepts/test.md",
            score=10.0,
            title="Test",
            snippet="A snippet",
            matched_terms=1,
            outgoing_links=3,
            backlinks=5,
        )
        d_set = hit_set.as_dict()
        self.assertIn("outgoing_links", d_set)
        self.assertIn("backlinks", d_set)
        self.assertEqual(d_set["outgoing_links"], 3)
        self.assertEqual(d_set["backlinks"], 5)

        hit_zeros = SearchHit(
            node="concepts/test.md",
            score=10.0,
            title="Test",
            snippet="A snippet",
            matched_terms=1,
            outgoing_links=0,
            backlinks=0,
        )
        d_zeros = hit_zeros.as_dict()
        self.assertEqual(d_zeros["outgoing_links"], 0)
        self.assertEqual(d_zeros["backlinks"], 0)


class LinkCountsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # alpha.md:
        # - links to beta.md twice (counts once)
        # - links to gamma.md
        # - self-link to alpha.md (excluded)
        # - broken link to missing.md (excluded)
        # - external link to https://example.com (excluded)
        write_doc(
            self.root,
            "concepts/alpha.md",
            "type: concept\ntitle: Alpha Sintering Device\ntags: [device, sintering]\ndescription: Alpha device.\n",
            "## Taxonomy\n"
            "Links to [Beta](beta.md) and again to [Beta Note](beta.md).\n"
            "Also links to [Gamma](gamma.md).\n"
            "Self link [Self](alpha.md).\n"
            "Broken link [Missing](missing.md).\n"
            "External link [Web](https://example.com/spec).\n",
        )

        # beta.md: no links
        write_doc(
            self.root,
            "concepts/beta.md",
            "type: concept\ntitle: Beta Sintering Material\ntags: [material, sintering]\ndescription: Beta material.\n",
            "## Taxonomy\nBeta material description without outgoing links.\n",
        )

        # gamma.md: links to beta.md
        write_doc(
            self.root,
            "concepts/gamma.md",
            "type: patent\ntitle: Gamma Sintering Process\ntags: [process, sintering]\ndescription: Gamma process.\n",
            "## Workflow\nLinks to [Beta](beta.md).\n",
        )

        # isolated.md: no links in or out (0/0)
        write_doc(
            self.root,
            "concepts/isolated.md",
            "type: concept\ntitle: Isolated Sintering Concept\ntags: [isolated, sintering]\ndescription: Isolated concept.\n",
            "## Taxonomy\nIsolated note with no incoming or outgoing links.\n",
        )

    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main([str(arg) for arg in args])
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_attach_link_counts_known_graph(self):
        backend = LocalSearchBackend(self.root)
        hits = backend.search("sintering", limit=10)
        self.assertEqual(len(hits), 4)

        attached = attach_link_counts(hits, self.root)
        counts = {h.node: (h.outgoing_links, h.backlinks) for h in attached}

        self.assertEqual(counts["concepts/alpha.md"], (2, 0))
        self.assertEqual(counts["concepts/beta.md"], (0, 2))
        self.assertEqual(counts["concepts/gamma.md"], (1, 1))
        self.assertEqual(counts["concepts/isolated.md"], (0, 0))

    def test_attach_link_counts_node_absent_keeps_none(self):
        missing_hit = SearchHit(
            node="concepts/nonexistent.md",
            score=5.0,
            title="Nonexistent",
            snippet="Does not exist",
            matched_terms=1,
        )
        attached = attach_link_counts([missing_hit], self.root)
        self.assertIsNone(attached[0].outgoing_links)
        self.assertIsNone(attached[0].backlinks)
        d = attached[0].as_dict()
        self.assertNotIn("outgoing_links", d)
        self.assertNotIn("backlinks", d)

    def test_invariance_score_order_breakdown_unchanged(self):
        backend = LocalSearchBackend(self.root)
        raw_hits = backend.search("sintering", limit=10)
        attached_hits = attach_link_counts(raw_hits, self.root)

        self.assertEqual(len(raw_hits), len(attached_hits))
        for raw, att in zip(raw_hits, attached_hits):
            self.assertEqual(raw.node, att.node)
            self.assertEqual(raw.score, att.score)
            self.assertEqual(raw.matched_terms, att.matched_terms)
            self.assertEqual(raw.is_fallback, att.is_fallback)
            self.assertEqual(raw.query_corrections, att.query_corrections)
            self.assertEqual(raw.breakdown, att.breakdown)
            if raw.breakdown is not None:
                self.assertEqual(raw.as_dict()["breakdown"]["total"], att.as_dict()["breakdown"]["total"])

    def test_cli_json_includes_link_counts_text_unchanged(self):
        # Text output unchanged
        code, out_text, _ = self.run_cli("search", self.root, "sintering")
        self.assertEqual(code, 0)
        self.assertNotIn("outgoing_links", out_text)
        self.assertNotIn("backlinks", out_text)

        # JSON output includes fields
        code, out_json, _ = self.run_cli("search", self.root, "sintering", "--json")
        self.assertEqual(code, 0)
        hits_data = json.loads(out_json)
        counts = {h["node"]: (h.get("outgoing_links"), h.get("backlinks")) for h in hits_data}
        self.assertEqual(counts["concepts/alpha.md"], (2, 0))
        self.assertEqual(counts["concepts/beta.md"], (0, 2))
        self.assertEqual(counts["concepts/gamma.md"], (1, 1))
        self.assertEqual(counts["concepts/isolated.md"], (0, 0))

    def test_mcp_dispatch_tool_okf_search_includes_fields(self):
        hits_data = dispatch_tool(
            "okf_search",
            {"wiki": str(self.root), "query": "sintering"},
            root=self.root,
        )
        counts = {h["node"]: (h.get("outgoing_links"), h.get("backlinks")) for h in hits_data}
        self.assertEqual(counts["concepts/alpha.md"], (2, 0))
        self.assertEqual(counts["concepts/beta.md"], (0, 2))
        self.assertEqual(counts["concepts/gamma.md"], (1, 1))
        self.assertEqual(counts["concepts/isolated.md"], (0, 0))

    def test_filter_independence(self):
        # Search all
        all_hits = dispatch_tool(
            "okf_search",
            {"wiki": str(self.root), "query": "sintering"},
            root=self.root,
        )
        counts_all = {h["node"]: (h["outgoing_links"], h["backlinks"]) for h in all_hits}

        # Search with --type concept (excludes gamma.md)
        concept_hits = dispatch_tool(
            "okf_search",
            {"wiki": str(self.root), "query": "sintering", "type": "concept"},
            root=self.root,
        )
        counts_filtered = {h["node"]: (h["outgoing_links"], h["backlinks"]) for h in concept_hits}

        # The counts for alpha, beta, isolated must remain identical even though gamma was filtered out
        for node in counts_filtered:
            self.assertEqual(
                counts_filtered[node],
                counts_all[node],
                f"Link counts for {node} changed when filtered!",
            )


if __name__ == "__main__":
    unittest.main()
