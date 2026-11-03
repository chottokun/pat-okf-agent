"""Release metadata consistency checks."""
from __future__ import annotations

import tomllib
import unittest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pat_okf_agent
from pat_okf_agent.wiki import mcp_server

ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_package_versions_match_pyproject(self):
        pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        expected = pyproject["project"]["version"]
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = next(
            package["version"] for package in lock["package"]
            if package["name"] == "pat-okf-agent")
        self.assertEqual(pat_okf_agent.__version__, expected)
        self.assertEqual(mcp_server.SERVER_VERSION, expected)
        try:
            installed = version("pat-okf-agent")
        except PackageNotFoundError:
            installed = expected
        self.assertEqual(installed, expected)
        self.assertEqual(locked, expected)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{expected}]", changelog)


if __name__ == "__main__":
    unittest.main()
