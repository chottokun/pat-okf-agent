"""Public-release safety checks for the tracked repository."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseTests(unittest.TestCase):
    def test_tracked_tree_passes_public_guard(self):
        result = subprocess.run(
            [sys.executable, "scripts/check_public_repo.py"],
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(
            result.returncode, 0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def test_examples_do_not_claim_epo_source(self):
        for path in (ROOT / "examples" / "wiki").rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("source: EPO OPS", text, msg=str(path))


if __name__ == "__main__":
    unittest.main()
