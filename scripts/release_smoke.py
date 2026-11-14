#!/usr/bin/env python3
"""Offline release smoke test for the OKF command line surface.

The script runs against a temporary wiki only.  It never performs network
access and never reads ``.secrets`` or ``data/external``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import okf_kb_agent


class SmokeFailure(RuntimeError):
    """A smoke-test expectation failed."""


def run_cli(*args: str) -> str:
    command = [
        sys.executable,
        "-m",
        "okf_kb_agent.wiki.cli",
        *args,
    ]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeFailure(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed.stdout


def run_json(*args: str) -> Any:
    output = run_cli(*args)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(
            f"expected JSON from: {' '.join(args)}\n"
            f"stdout:\n{output}"
        ) from exc


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="okf-release-smoke-") as raw:
        reported = run_cli("--version").strip()
        try:
            expected_ver = version("okf-kb-agent")
        except PackageNotFoundError:
            expected_ver = okf_kb_agent.__version__
        expect(reported == expected_ver,
               f"okf --version {reported!r} does not match package metadata")

        root = Path(raw) / "wiki"
        skills = root / ".agents" / "skills"
        history = root / ".agents" / "flow_history.jsonl"

        skills.mkdir(parents=True)
        (skills / "demo" / "SKILL.md").parent.mkdir()
        (skills / "demo" / "SKILL.md").write_text(
            "---\n"
            "name: demo\n"
            "description: Release smoke demo skill\n"
            "trigger_conditions:\n"
            "  - demo\n"
            "---\n"
            "\n"
            "# Demo\n",
            encoding="utf-8",
        )

        run_json("init", str(root), "--skills-dir", str(skills), "--json")
        run_cli("validate", str(root))
        run_cli("index", str(root), "--check")

        schema = run_json("schema", "--json")
        expect(isinstance(schema, list) and schema, "schema must be non-empty")

        skills_payload = run_json(
            "skills", str(root), "--skills-dir", str(skills),
            "--check", "--json")
        expect(skills_payload["ok"], "demo skill must validate")
        expect(len(skills_payload["skills"]) == 1,
               "only the demo skill should be discovered")

        records = [
            {"task": "smoke flow", "steps": [
                "step one", "step two", "step three"],
             "outcome": "success"}
            for _ in range(2)
        ]
        history.write_text(
            "\n".join(json.dumps(row) for row in records) + "\n",
            encoding="utf-8",
        )

        proposed = run_json(
            "evolve", str(root), "--history", str(history),
            "--propose", "--json")
        expect(len(proposed["written"]) == 0,
               "propose must not write a live skill")
        expect(len(proposed["proposals"]) == 1,
               "propose must write one proposal")
        proposal = Path(proposed["proposals"][0])
        expect(proposal.parent.name == ".proposals",
               "proposal must live under .proposals")
        expect(not (skills / "smoke-flow" / "SKILL.md").exists(),
               "propose must not create the live skill")

        discovered = run_json(
            "skills", str(root), "--skills-dir", str(skills),
            "--check", "--json")
        expect(discovered["ok"], "skills must stay valid after propose")
        expect(len(discovered["skills"]) == 1,
               "proposals must not be discovered as skills")

        applied = run_json(
            "evolve", str(root), "--history", str(history),
            "--apply", "--json")
        expect(len(applied["written"]) == 1,
               "apply must create one skill")
        expect((skills / "smoke-flow" / "SKILL.md").exists(),
               "applied skill is missing")

        guarded = run_json(
            "evolve", str(root), "--history", str(history),
            "--propose", "--json")
        expect(guarded["proposals"] == [],
               "existing proposal must not be overwritten")
        expect(guarded["clusters"][0]["proposal"]["reason"]
               in {"proposal already exists", "proposal unchanged"},
               "proposal guard must explain why it did not write")

        refreshed = run_json(
            "evolve", str(root), "--history", str(history),
            "--propose", "--force", "--json")
        expect(len(refreshed["proposals"]) == 1,
               "explicit --force must refresh the proposal")

        again = run_json(
            "evolve", str(root), "--history", str(history),
            "--apply", "--json")
        expect(again["written"] == [],
               "apply must not overwrite an existing skill")

        print("release smoke: ok")
        print(f"  temp wiki: {root}")
        print(f"  proposal:  {proposal}")
        print(f"  skill:     {skills / 'smoke-flow' / 'SKILL.md'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as exc:
        print(f"release smoke: FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
