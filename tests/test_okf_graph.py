"""Tests for the OKF relative-link graph and weighted traversal."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.graph import WikiGraph, build_graph
from pat_okf_agent.wiki.model import SCOPE_WEIGHTS


def write(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "a.md", "type: Note\ntitle: A\n", "[b](b.md)\n")
        write(self.root, "b.md", "type: Gotcha\ntitle: B\n", "[c](c.md)\n")
        write(self.root, "c.md", "type: Note\ntitle: C\n", "")
        write(self.root, "nested/d.md", "type: Note\ntitle: D\n", "")
        self.graph = build_graph(self.root)

    def test_edges_and_backlinks(self):
        self.assertEqual(self.graph.edges["a.md"], ["b.md"])
        self.assertEqual(self.graph.backlinks["b.md"], ["a.md"])
        self.assertEqual(self.graph.node("a.md").title, "A")
        self.assertEqual(self.graph.broken, [])

    def test_weighted_traversal_gotcha_ranks_first(self):
        hits = self.graph.traverse(["a.md"], depth=2)
        self.assertEqual([h.node for h in hits], ["b.md", "c.md", "a.md"])
        self.assertAlmostEqual(hits[0].score, 0.85 * SCOPE_WEIGHTS["gotchas"])
        self.assertAlmostEqual(hits[1].score, 0.85 ** 2 * SCOPE_WEIGHTS["gotchas"])
        self.assertAlmostEqual(hits[2].score, 1.0)
        self.assertEqual(hits[1].path, ("a.md", "b.md", "c.md"))

    def test_depth_limit(self):
        hits = self.graph.traverse(["a.md"], depth=1)
        self.assertEqual([h.node for h in hits], ["b.md", "a.md"])

    def test_damping_and_scope_weights_are_configurable(self):
        hits = self.graph.traverse(
            ["a.md"], depth=2, gamma=0.5,
            scope_weights={"taxonomy": 3.0, "gotchas": 1.0,
                           "workflow": 1.0, "synergistic": 1.0},
        )
        scores = {h.node: h.score for h in hits}
        self.assertAlmostEqual(scores["b.md"], 0.5)
        self.assertAlmostEqual(scores["c.md"], 0.5 * 3.0 * 0.5)

    def test_direction_in_follows_backlinks(self):
        hits = self.graph.traverse(["b.md"], depth=1, direction="in")
        self.assertEqual([h.node for h in hits], ["a.md", "b.md"])
        # The a -> b link falls back to b's Gotcha scope (weight 2.0),
        # so the backlink can outrank the seed under the plan's formula.
        self.assertAlmostEqual(hits[0].score, 0.85 * SCOPE_WEIGHTS["gotchas"])
        self.assertAlmostEqual(hits[1].score, 1.0)

    def test_direction_both(self):
        hits = self.graph.traverse(["b.md"], depth=1, direction="both")
        self.assertEqual([h.node for h in hits], ["a.md", "b.md", "c.md"])

    def test_cycles_terminate_and_max_nodes_is_respected(self):
        write(self.root, "b.md", "type: Gotcha\n", "[a](a.md)\n")
        graph = build_graph(self.root)
        hits = graph.traverse(["a.md"], depth=10)
        self.assertEqual(sorted(h.node for h in hits), ["a.md", "b.md"])
        self.assertEqual(len(hits), len({h.node for h in hits}))
        self.assertEqual(len(graph.traverse(["a.md"], depth=10, max_nodes=1)),
                         1)

    def test_resolve_seed_by_relative_path_or_stem(self):
        self.assertEqual(self.graph.resolve_seed("a.md"), "a.md")
        self.assertEqual(self.graph.resolve_seed("a"), "a.md")
        self.assertEqual(self.graph.resolve_seed("B"), "b.md")
        self.assertEqual(self.graph.resolve_seed("nested/d"), "nested/d.md")
        with self.assertRaises(KeyError):
            self.graph.resolve_seed("nope")

    def test_broken_links_are_reported_not_followed(self):
        write(self.root, "a.md", "type: Note\n", "[lost](missing.md)\n")
        graph = build_graph(self.root)
        self.assertEqual(graph.broken, [("a.md", "missing.md")])
        self.assertEqual(graph.edges["a.md"], [])

    def test_duplicate_links_collapse(self):
        write(self.root, "a.md", "type: Note\n",
              "[b](b.md)\n\n[b again](b.md)\n")
        graph = build_graph(self.root)
        self.assertEqual(graph.edges["a.md"], ["b.md"])

    def test_edge_scope_comes_from_heading_not_target_type(self):
        write(self.root, "a.md", "type: Note\n",
              "## Taxonomy\n[gotcha](b.md)\n")
        graph = build_graph(self.root)
        edge = graph.edge_objects["a.md"][0]
        self.assertEqual(edge.heading, "Taxonomy")
        self.assertEqual(edge.scope, "taxonomy")
        hits = graph.traverse(["a.md"], depth=1)
        self.assertAlmostEqual(
            {hit.node: hit.score for hit in hits}["b.md"], 0.85)

    def test_query_relevance_boosts_matching_anchor_text(self):
        write(self.root, "a.md", "type: Note\n",
              "## Gotchas / Anti-Pitfalls\n"
              "[amorphous failure](b.md)\n")
        graph = build_graph(self.root)
        matched = graph.traverse(["a.md"], depth=1, query="amorphous")
        unrelated = graph.traverse(["a.md"], depth=1, query="unrelated")
        matched_b = {h.node: h.score for h in matched}["b.md"]
        unrelated_b = {h.node: h.score for h in unrelated}["b.md"]
        self.assertAlmostEqual(matched_b, 0.85 * SCOPE_WEIGHTS["gotchas"])
        self.assertLess(unrelated_b, matched_b)
        self.assertGreater(unrelated_b, 0.0)

    def test_scope_filter_keeps_seed_and_only_requested_scopes(self):
        hits = self.graph.traverse(["a.md"], depth=2, scopes={"gotchas"})
        self.assertEqual([h.node for h in hits], ["b.md", "a.md"])
        with self.assertRaises(ValueError):
            self.graph.traverse(["a.md"], scopes={"nope"})

    def test_keyword_arguments_validate(self):
        with self.assertRaises(ValueError):
            self.graph.traverse(["a.md"], direction="sideways")
        with self.assertRaises(ValueError):
            self.graph.traverse(["a.md"], gamma=1.5)
        with self.assertRaises(ValueError):
            self.graph.traverse(["a.md"], depth=-1)
        with self.assertRaises(ValueError):
            self.graph.traverse(["a.md"], max_nodes=0)

    def test_empty_graph(self):
        with tempfile.TemporaryDirectory() as empty:
            graph = build_graph(Path(empty))
            self.assertIsInstance(graph, WikiGraph)
            self.assertEqual(graph.nodes, {})
            self.assertEqual(graph.traverse([]), [])


if __name__ == "__main__":
    unittest.main()
