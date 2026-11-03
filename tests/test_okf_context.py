"""Tests for deterministic context resolution and Gotchas filtering."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from pat_okf_agent.wiki.cli import main
from pat_okf_agent.wiki.context import (
    PHASE_TOKEN_BUDGETS,
    ContextResolver,
    clip_to_tokens,
    estimate_tokens,
    has_constraint_violation,
)


def write(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "index.md", "type: Index\n", "# Knowledge Index\n")
        write(
            self.root, "seeds/amorphous.md", "type: Note\ntitle: Amorphous alloy\n",
            "# Amorphous alloy\n\n"
            "## Taxonomy\n"
            "[Background](background.md)\n\n"
            "## Gotchas / Anti-Pitfalls\n"
            "[safe gotcha](../gotchas/safe.md)\n"
            "[unsafe gotcha](../gotchas/unsafe.md)\n",
        )
        write(
            self.root, "seeds/background.md", "type: Note\ntitle: Background\n",
            "# Background\n\nAmorphous background details.\n",
        )
        write(
            self.root, "gotchas/safe.md", "type: Gotcha\ntitle: Safe gotcha\n",
            "# Safe gotcha\n\n## Gotchas / Anti-Pitfalls\nUse inert gas.\n",
        )
        write(
            self.root, "gotchas/unsafe.md", "type: Gotcha\ntitle: Unsafe\n",
            "# Unsafe\n\n## Gotchas / Anti-Pitfalls\n"
            "INCOMPATIBLE_EXPLICIT: do not use this route.\n",
        )
        write(
            self.root, "gotchas/windows.md", "type: Gotcha\ntitle: Windows\n",
            "# Windows\n\n## Gotchas / Anti-Pitfalls\n"
            "INCOMPATIBLE_WITH: windows, win32\n",
        )

    def resolver(self, **kwargs) -> ContextResolver:
        return ContextResolver(self.root, **kwargs)

    def test_resolve_prunes_incompatible_gotcha_and_keeps_safe(self):
        bundle = self.resolver().resolve("amorphous", max_nodes=5)
        nodes = [node.node for node in bundle.nodes]
        self.assertIn("seeds/amorphous.md", nodes)
        self.assertIn("gotchas/safe.md", nodes)
        self.assertNotIn("gotchas/unsafe.md", nodes)
        self.assertIn("gotchas/unsafe.md", bundle.pruned)
        self.assertEqual(bundle.seeds[0], "seeds/amorphous.md")
        self.assertLessEqual(bundle.tokens, bundle.token_limit)

    def test_soft_constraint_is_query_dependent(self):
        self.assertTrue(has_constraint_violation(
            "deploy on windows", "INCOMPATIBLE_WITH: windows, win32"))
        self.assertFalse(has_constraint_violation(
            "deploy on linux", "INCOMPATIBLE_WITH: windows, win32"))
        self.assertTrue(has_constraint_violation(
            "anything", "INCOMPATIBLE_EXPLICIT"))

    def test_token_budget_truncates_context(self):
        bundle = self.resolver().resolve("amorphous", token_limit=12)
        self.assertLessEqual(bundle.tokens, 12)
        self.assertTrue(bundle.truncated)
        self.assertTrue(bundle.text)

    def test_progressive_phases_respect_budgets(self):
        phases = self.resolver().progressive("amorphous")
        self.assertEqual(set(phases), {"phase1", "phase2", "phase3"})
        for phase, budget in PHASE_TOKEN_BUDGETS.items():
            self.assertLessEqual(phases[phase]["tokens"], budget)
            self.assertLessEqual(phases[phase]["token_limit"], budget)

    def test_clip_and_estimate_helpers(self):
        text = "alpha beta gamma delta"
        clipped, was_clipped = clip_to_tokens(text, 2)
        self.assertTrue(was_clipped)
        self.assertLessEqual(estimate_tokens(clipped), 2)
        self.assertEqual(estimate_tokens(""), 0)

    def test_cli_context_json(self):
        out = StringIO()
        with redirect_stdout(out):
            code = main(["context", str(self.root), "amorphous", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        nodes = [node["node"] for node in data["nodes"]]
        self.assertIn("seeds/amorphous.md", nodes)
        self.assertNotIn("gotchas/unsafe.md", nodes)


if __name__ == "__main__":
    unittest.main()
