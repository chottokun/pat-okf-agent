"""Tests for the ``okf`` command line interface."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pat_okf_agent
from pat_okf_agent.wiki.cli import main


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def node(meta: str, body: str = "# Note\n") -> str:
    return f"---\n{meta}---\n{body}"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "wiki"

    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(arg) for arg in args])
        return code, out.getvalue(), err.getvalue()

    def test_version_flag_uses_package_version(self):
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), pat_okf_agent.__version__)

    def test_init_creates_index_log_and_agent_instructions(self):
        skills = Path(self.tmp.name) / "skills"
        write(skills, "demo/SKILL.md",
              node("name: demo\ndescription: Demo skill for testing\n"
                   "trigger_conditions: [patent, epo]\n"))
        code, _, _ = self.run_cli("init", self.root, "--skills-dir", skills)
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "index.md").exists())
        self.assertTrue((self.root / "log.md").exists())
        agents = (self.root / ".agents/AGENTS.md").read_text("utf-8")
        self.assertIn("demo", agents)
        self.assertIn("Demo skill for testing", agents)
        self.assertIn("patent, epo", agents)
        self.assertIn("index.md", agents)
        self.assertIn("<!-- LEARNED -->", agents)
        self.assertIn("<!-- NO_AUTO_MUTATE -->", agents)
        self.assertIn("create-skill", agents)
        self.assertIn("task_memory.md", agents)
        self.assertTrue((self.root / ".agents/skills/README.md").exists())
        self.assertTrue((self.root / ".agents/task_memory.md").exists())

    def test_init_writes_relative_skill_locations(self):
        skills = self.root / ".agents" / "skills"
        write(skills, "demo/SKILL.md",
              node("name: demo\ndescription: Demo skill for testing\n"
                   "trigger_conditions: [patent]\n"))
        code, _, _ = self.run_cli("init", self.root, "--skills-dir", skills)
        self.assertEqual(code, 0)
        agents = (self.root / ".agents/AGENTS.md").read_text("utf-8")
        self.assertIn(".agents/skills/demo/SKILL.md", agents)
        self.assertNotIn(str(self.root.resolve()), agents)

    def test_init_preserves_learned_tail(self):
        code, _, _ = self.run_cli("init", self.root)
        self.assertEqual(code, 0)
        agents = self.root / ".agents/AGENTS.md"
        agents.write_text(agents.read_text("utf-8") +
                          "\n- Learned lesson: keep anchors descriptive.\n",
                          encoding="utf-8")
        code, _, _ = self.run_cli("init", self.root)
        self.assertEqual(code, 0)
        text = agents.read_text("utf-8")
        self.assertIn("Learned lesson: keep anchors descriptive.", text)
        self.assertEqual(text.count("\n<!-- LEARNED -->"), 1)
        self.assertEqual(text.count("Dynamic runtime additions"), 1)

    def test_index_check_and_write(self):
        write(self.root, "notes/a.md", node("type: Note\ntitle: A\n"))
        code, _, _ = self.run_cli("index", self.root, "--check")
        self.assertEqual(code, 1)
        code, _, _ = self.run_cli("index", self.root)
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "notes/index.md").exists())
        code, _, _ = self.run_cli("index", self.root, "--check")
        self.assertEqual(code, 0)

    def test_validate_exit_codes(self):
        write(self.root, "index.md", "# Index\n")
        write(self.root, "a.md", node("type: Note\n"))
        code, out, _ = self.run_cli("validate", self.root, "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        write(self.root, "b.md", node("title: no type\n"))
        code, out, _ = self.run_cli("validate", self.root, "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_search_output_and_json(self):
        write(self.root, "a.md",
              node("type: Note\ntitle: Amorphous alloy\n",
                   "A metallic glass.\n"))
        code, out, _ = self.run_cli("search", self.root, "amorphous")
        self.assertEqual(code, 0)
        self.assertIn("a.md", out)
        code, out, _ = self.run_cli("search", self.root, "amorphous", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["node"], "a.md")
        code, out, _ = self.run_cli(
            "search", self.root, "amorphous", "--backend", "local", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["node"], "a.md")

    def test_sqlite_backend_cli_roundtrip(self):
        write(self.root, "a.md",
              node("type: Note\ntitle: Amorphous alloy\n",
                   "# Amorphous alloy\nMetallic glass.\n"))
        db = Path(self.tmp.name) / "test.sqlite3"
        code, out, _ = self.run_cli(
            "sqlite", self.root, "--db", db, "--json")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["documents"], 1)
        self.assertEqual(summary["edges"], 0)
        code, out, _ = self.run_cli(
            "search", self.root, "amorphous", "--backend", "sqlite",
            "--db", db, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["node"], "a.md")

    def test_traverse_and_stats(self):
        write(self.root, "a.md", node("type: Note\n", "[b](b.md)\n"))
        write(self.root, "b.md", node("type: Gotcha\n"))
        code, out, _ = self.run_cli("traverse", self.root, "a", "--json")
        self.assertEqual(code, 0)
        hits = json.loads(out)
        self.assertEqual(hits[0]["node"], "b.md")
        self.assertAlmostEqual(hits[0]["score"], 1.7)
        code, out, _ = self.run_cli("stats", self.root, "--json")
        self.assertEqual(code, 0)
        stats = json.loads(out)
        self.assertEqual(stats["concept_count"], 2)
        self.assertEqual(stats["pattern"], "A")
        self.assertEqual(stats["scopes"]["gotchas"], 1)
        self.assertEqual(stats["basis"], {"external": 0, "model": 0, "mixed": 0, "undeclared": 2})

        code, text_out, _ = self.run_cli("stats", self.root)
        self.assertEqual(code, 0)
        self.assertIn("basis: external=0  model=0  mixed=0  undeclared=2", text_out)

    def test_validate_strict_provenance_flag(self):
        write(self.root, "index.md", "# Index\n")
        # basis: external without sources is a P2 warning normally
        write(self.root, "a.md", node("type: Concept\nbasis: external\nverified_at: '2026-11-07'\n", "# A\n"))
        code, out, _ = self.run_cli("validate", self.root)
        self.assertEqual(code, 0)

        code_strict, out_strict, _ = self.run_cli("validate", self.root, "--strict-provenance")
        self.assertEqual(code_strict, 1)

    def test_init_json_and_multiple_skill_dirs(self):
        first = Path(self.tmp.name) / "skills-a"
        second = Path(self.tmp.name) / "skills-b"
        write(first, "demo/SKILL.md",
              node("name: demo\ndescription: First skill\n"
                   "trigger_conditions:\n  - patent\n  - 特許\n"))
        write(second, "extra/SKILL.md",
              node("name: extra\ndescription: Second skill\n"
                   "trigger_conditions: [wiki]\n"))
        code, out, _ = self.run_cli(
            "init", self.root, "--skills-dir", first,
            "--skills-dir", second, "--json")
        self.assertEqual(code, 0)
        paths = json.loads(out)
        self.assertIn("index", paths)
        self.assertIn("agents", paths)
        agents = (self.root / ".agents/AGENTS.md").read_text("utf-8")
        self.assertIn("First skill", agents)
        self.assertIn("Second skill", agents)
        self.assertIn("patent, 特許", agents)

    def test_index_json_contract(self):
        write(self.root, "notes/a.md", node("type: Note\ntitle: A\n"))
        code, out, _ = self.run_cli("index", self.root, "--check", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("index.md", json.loads(out)["stale"])
        code, out, _ = self.run_cli("index", self.root, "--json")
        self.assertEqual(code, 0)
        written = json.loads(out)
        self.assertTrue(written["ok"])
        self.assertEqual(written["count"], len(written["written"]))
        code, out, _ = self.run_cli("index", self.root, "--check", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_skills_discovery_and_check(self):
        skills = Path(self.tmp.name) / "skills"
        write(skills, "demo/SKILL.md",
              node("name: demo\ndescription: Demo skill\n"
                   "trigger_conditions:\n  - demo\n"))
        code, out, _ = self.run_cli(
            "skills", self.root, "--skills-dir", skills,
            "--check", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["skills"][0]["triggers"], "demo")
        write(skills, "broken/SKILL.md",
              node("name: broken\ndescription: No triggers\n"))
        code, out, _ = self.run_cli(
            "skills", self.root, "--skills-dir", skills,
            "--check", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertEqual(len(json.loads(out)["missing"]), 1)
        write(skills, "invalid/SKILL.md", "not frontmatter\n")
        code, out, _ = self.run_cli(
            "skills", self.root, "--skills-dir", skills,
            "--check", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        invalid = [spec for spec in payload["skills"]
                   if "missing YAML frontmatter" in spec.get("error", "")]
        self.assertEqual(len(invalid), 1)
        self.assertIn("missing YAML frontmatter", invalid[0]["error"])
        write(skills, "scalar/SKILL.md",
              node("name: scalar\ndescription: Scalar triggers\n"
                   "trigger_conditions: demo\n"))
        code, out, _ = self.run_cli(
            "skills", self.root, "--skills-dir", skills,
            "--check", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        scalar = [spec for spec in payload["skills"]
                  if spec["name"] == "scalar"][0]
        self.assertIn("must be a list", scalar["error"])

    def test_schema_lists_mcp_tools(self):
        code, out, _ = self.run_cli("schema", "--json")
        self.assertEqual(code, 0)
        specs = json.loads(out)
        self.assertEqual(len(specs), 7)
        self.assertIn("okf_context", {spec["name"] for spec in specs})
        self.assertEqual(specs[0]["inputSchema"]["type"], "object")


    def test_synonym_expansion_and_evaluation_cli(self):
        write(self.root, "a.md",
              node("type: Note\ntitle: Glass note\n",
                   "# Glass note\nMetallic glass cools rapidly.\n"))
        write(self.root, "synonyms.json", json.dumps([
            ["amorphous alloy", "metallic glass"],
        ]))
        code, out, _ = self.run_cli(
            "search", self.root, "amorphous alloy", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])
        code, out, _ = self.run_cli(
            "search", self.root, "amorphous alloy", "--expand", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["node"], "a.md")
        write(self.root, "eval_queries.json", json.dumps([
            {"query": "amorphous alloy", "expected": ["missing.md"]},
        ]))
        code, out, _ = self.run_cli(
            "eval-synonyms", self.root, "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["recommended_pattern"], "C")
        code, _, _ = self.run_cli(
            "eval-synonyms", self.root, "--check")
        self.assertEqual(code, 1)

    def test_evolve_is_dry_run_by_default_and_apply_is_guarded(self):
        history = Path(self.tmp.name) / "flows.jsonl"
        rows = [
            {"task": "ingest patent", "steps": [
                "epo check", "okf ingest-epo", "okf validate"]}
            for _ in range(3)
        ]
        history.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                           encoding="utf-8")
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["records"], 3)
        self.assertEqual(payload["written"], [])
        path = Path(payload["clusters"][0]["path"])
        self.assertFalse(path.exists())
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history,
            "--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["written"], [])
        code, _, err = self.run_cli(
            "evolve", self.root, "--history", history,
            "--dry-run", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history, "--apply", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(path.exists())
        path.write_text("user-authored\n", encoding="utf-8")
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history, "--apply", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["written"], [])
        self.assertEqual(path.read_text("utf-8"), "user-authored\n")

    def test_evolve_propose_is_non_destructive_and_not_a_skill(self):
        history = Path(self.tmp.name) / "flows.jsonl"
        rows = [
            {"task": "ingest patent", "steps": [
                "epo check", "okf ingest-epo", "okf validate"]}
            for _ in range(3)
        ]
        history.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                           encoding="utf-8")
        skill = write(
            self.root, ".agents/skills/ingest-patent/SKILL.md",
            node("name: ingest\ndescription: Existing skill\n"
                 "trigger_conditions: [ingest]\n",
                 "# Existing\n\n## Steps\n\n1. `old step`\n"))
        original = skill.read_text("utf-8")
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history,
            "--propose", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["written"], [])
        self.assertEqual(len(payload["proposals"]), 1)
        proposal = Path(payload["proposals"][0])
        self.assertEqual(proposal.parent.name, ".proposals")
        self.assertTrue(proposal.exists())
        self.assertEqual(skill.read_text("utf-8"), original)
        text = proposal.read_text("utf-8")
        self.assertIn("1. `old step`", text)
        self.assertIn("epo check", text)
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history,
            "--propose", "--json")
        self.assertEqual(code, 0)
        unchanged = json.loads(out)
        self.assertEqual(unchanged["proposals"], [])
        self.assertFalse(
            unchanged["clusters"][0]["proposal"]["written"])
        proposal.write_text("review notes\n", encoding="utf-8")
        code, _, err = self.run_cli(
            "evolve", self.root, "--history", history,
            "--propose", "--learn")
        self.assertEqual(code, 2)
        self.assertIn("requires --apply", err)
        self.assertEqual(proposal.read_text("utf-8"), "review notes\n")
        code, _, err = self.run_cli(
            "evolve", self.root, "--history", history, "--force")
        self.assertEqual(code, 2)
        self.assertIn("requires --apply or --propose", err)
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history,
            "--propose", "--force", "--json")
        self.assertEqual(code, 0)
        forced = json.loads(out)
        self.assertEqual(len(forced["proposals"]), 1)
        self.assertEqual(
            forced["clusters"][0]["proposal"]["reason"], "proposal updated")
        self.assertIn("## Review checklist", proposal.read_text("utf-8"))
        code, out, _ = self.run_cli(
            "skills", self.root, "--check", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["skills"]), 1)
        code, _, err = self.run_cli(
            "evolve", self.root, "--history", history,
            "--dry-run", "--propose")
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)

    def test_evolve_apply_and_propose_can_combine(self):
        history = Path(self.tmp.name) / "combined.jsonl"
        row = {"task": "new flow", "steps": [
            "first", "second", "third"]}
        history.write_text((json.dumps(row) + "\n") * 2, encoding="utf-8")
        code, out, _ = self.run_cli(
            "evolve", self.root, "--history", history,
            "--apply", "--propose", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["written"]), 1)
        self.assertEqual(len(payload["proposals"]), 1)
        self.assertTrue(Path(payload["written"][0]).exists())
        self.assertTrue(Path(payload["proposals"][0]).exists())
        code, out, _ = self.run_cli(
            "skills", self.root, "--check", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["skills"]), 1)

    def test_qdrant_cli_requires_embedding_url_and_calls_sync(self):
        code, _, err = self.run_cli("qdrant", self.root, "--json")
        self.assertEqual(code, 2)
        self.assertIn("--embedding-url", err)

        class FakeBackend:
            def sync(self, *, force=False):
                return {"indexed": 1, "unchanged": 0, "removed": 0,
                        "points": 1, "dimensions": 4}

        with patch("pat_okf_agent.wiki.cli.get_backend",
                   return_value=FakeBackend()) as factory:
            code, out, _ = self.run_cli(
                "qdrant", self.root, "--embedding-url", "http://embed",
                "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["points"], 1)
        self.assertEqual(factory.call_args.args[0], "qdrant")

    def test_unknown_seed_is_reported(self):
        write(self.root, "a.md", node("type: Note\n"))
        code, _, err = self.run_cli("traverse", self.root, "nope")
        self.assertEqual(code, 2)
        self.assertIn("seed not found", err)

    def test_verify_source_verified_exit_0_json(self):
        from pat_okf_agent.wiki.verify import (
            EvidenceRecord,
            SourceMetadata,
            VerificationChecks,
            VerificationResult,
        )

        dummy_res = VerificationResult(
            ok=True,
            status="verified",
            input="paper:10.1111/jace.14748",
            identifier="10.1111/jace.14748",
            identifier_type="doi",
            kind="paper",
            registration_agency="crossref",
            via="crossref",
            retrieved_at="2026-11-08",
            issued="2017-01-12",
            metadata=SourceMetadata(title="AlN Paper", publisher="Wiley", year=2017),
            checks=VerificationChecks(title_match=True),
            evidence=EvidenceRecord(sha256="sha256:abc"),
            okf_source="paper:10.1111/jace.14748 issued=2017-01-12 retrieved=2026-11-08 via=crossref",
        )

        with patch("pat_okf_agent.wiki.cli.SourceVerifier.verify", return_value=dummy_res):
            code, out, err = self.run_cli("verify-source", "paper:10.1111/jace.14748", "--json")
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "verified")
            self.assertEqual(data["okf_source"], dummy_res.okf_source)

            # Plain text output
            code_txt, out_txt, _ = self.run_cli("verify-source", "paper:10.1111/jace.14748")
            self.assertEqual(code_txt, 0)
            self.assertIn("status: verified", out_txt)
            self.assertIn("paper:10.1111/jace.14748", out_txt)

    def test_verify_source_not_found_or_mismatch_exit_1(self):
        from pat_okf_agent.wiki.verify import (
            EvidenceRecord,
            SourceMetadata,
            VerificationChecks,
            VerificationResult,
        )

        mismatch_res = VerificationResult(
            ok=False,
            status="mismatch",
            input="10.1111/jace.14748",
            identifier="10.1111/jace.14748",
            identifier_type="doi",
            kind="paper",
            registration_agency="crossref",
            via="crossref",
            retrieved_at="2026-11-08",
            issued=None,
            metadata=SourceMetadata(title="AlN Paper", year=2017),
            checks=VerificationChecks(year_match=False),
            evidence=EvidenceRecord(),
            okf_source=None,
        )

        with patch("pat_okf_agent.wiki.cli.SourceVerifier.verify", return_value=mismatch_res):
            code, out, _ = self.run_cli("verify-source", "10.1111/jace.14748", "--expect-year", "1900", "--json")
            self.assertEqual(code, 1)
            data = json.loads(out)
            self.assertFalse(data["ok"])
            self.assertEqual(data["status"], "mismatch")

    def test_verify_source_error_exit_2(self):
        from pat_okf_agent.wiki.verify import SourceOfflineError

        with patch("pat_okf_agent.wiki.cli.SourceVerifier.verify", side_effect=SourceOfflineError("cache miss")):
            code, out, err = self.run_cli("verify-source", "10.1111/uncached", "--offline")
            self.assertEqual(code, 2)
            self.assertIn("error: cache miss", err)

        # Invalid target raises ValueError -> exit 2
        code_inv, out_inv, err_inv = self.run_cli("verify-source", "not-a-valid-target")
        self.assertEqual(code_inv, 2)
        self.assertIn("error:", err_inv)

    def test_verify_source_orphan_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            snapshots_dir = cache_dir / "snapshots"
            snapshots_dir.mkdir(parents=True)

            url = "https://example.com/item.html"
            from pat_okf_agent.wiki.verify import snapshot_key_for_url
            key = snapshot_key_for_url(url)
            meta_path = snapshots_dir / f"{key}.meta.json"
            meta_path.write_text(json.dumps({"url": url, "sha256": "abc"}, indent=2), encoding="utf-8")

            # Update orphan reason via CLI
            code, out, err = self.run_cli(
                "verify-source", url, "--orphan-reason", "数値なしのため不採用",
                "--cache-dir", str(cache_dir), "--json"
            )
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data.get("orphan_reason"), "数値なしのため不採用")

            # Missing meta raises SourceVerifyError -> exit 2
            code_missing, out_m, err_m = self.run_cli(
                "verify-source", "https://example.com/missing.html",
                "--orphan-reason", "reason", "--cache-dir", str(cache_dir)
            )
            self.assertEqual(code_missing, 2)
            self.assertIn("error:", err_m)

    def test_validate_check_evidence_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_dir = Path(tmpdir) / "wiki"
            wiki_dir.mkdir()
            write(wiki_dir, "index.md", "# Index\n")
            url = "https://example.com/aln.html"
            write(wiki_dir, "concepts/aln.md", node(
                "type: Concept\n"
                "title: 'AlN'\n"
                "basis: external\n"
                f"sources: ['web:{url} retrieved=2026-11-09']\n"
                "verified_at: '2026-11-09'\n",
                "# AlN\n",
            ))

            evidence_dir = Path(tmpdir) / "evidence"
            snapshots_dir = evidence_dir / "snapshots"
            snapshots_dir.mkdir(parents=True)

            from pat_okf_agent.wiki.verify import snapshot_key_for_url
            key = snapshot_key_for_url(url)
            (snapshots_dir / f"{key}.html").write_bytes(b"content")
            (snapshots_dir / f"{key}.meta.json").write_text(
                json.dumps({"url": url, "sha256": "sha256:123"}, indent=2), encoding="utf-8"
            )

            # Orphan with reason
            orphan_url = "https://example.com/orphan.html"
            o_key = snapshot_key_for_url(orphan_url)
            (snapshots_dir / f"{o_key}.html").write_bytes(b"orphan body")
            (snapshots_dir / f"{o_key}.meta.json").write_text(
                json.dumps({
                    "url": orphan_url,
                    "sha256": "sha256:456",
                    "orphan_reason": "数値なしのため不採用",
                }, indent=2),
                encoding="utf-8",
            )

            # 1. JSON mode
            code, out, err = self.run_cli(
                "validate", str(wiki_dir),
                "--check-evidence",
                "--evidence-dir", str(evidence_dir),
                "--json",
            )
            self.assertEqual(code, 0, f"err: {err}")
            data = json.loads(out)
            self.assertTrue(data["ok"])
            self.assertIn("evidence", data)
            ev = data["evidence"]
            self.assertEqual(ev["referenced_count"], 1)
            self.assertEqual(ev["missing_count"], 0)
            self.assertEqual(ev["orphan_count"], 1)
            self.assertEqual(ev["unexplained_orphan_count"], 0)

            # 2. Text output mode
            code_txt, out_txt, _ = self.run_cli(
                "validate", str(wiki_dir),
                "--check-evidence",
                "--evidence-dir", str(evidence_dir),
            )
            self.assertEqual(code_txt, 0)
            self.assertIn("OK: 1 concept(s)", out_txt)
            self.assertIn("EVIDENCE: 1 referenced (0 missing), 1 orphan(s) (1 with reason, 0 unexplained), 0 tombstone(s)", out_txt)
            self.assertIn(f"ORPHAN: {o_key} ({orphan_url}) — 数値なしのため不採用", out_txt)

            # 3. Missing snapshot with --strict-provenance -> exit 1
            missing_wiki = Path(tmpdir) / "missing_wiki"
            missing_wiki.mkdir()
            write(missing_wiki, "index.md", "# Index\n")
            write(missing_wiki, "concepts/missing.md", node(
                "type: Concept\n"
                "title: 'Missing'\n"
                "basis: external\n"
                "sources: ['web:https://example.com/not-cached.html retrieved=2026-11-09']\n"
                "verified_at: '2026-11-09'\n",
                "# Missing\n",
            ))
            code_fail, out_fail, _ = self.run_cli(
                "validate", str(missing_wiki),
                "--check-evidence",
                "--evidence-dir", str(evidence_dir),
                "--strict-provenance",
            )
            self.assertEqual(code_fail, 1)
            self.assertIn("FAILED:", out_fail)
            self.assertIn("[E1] missing snapshot metadata", out_fail)

            # 4. Nonexistent evidence-dir -> exit 2
            code_err, out_err, err_err = self.run_cli(
                "validate", str(wiki_dir),
                "--check-evidence",
                "--evidence-dir", str(Path(tmpdir) / "no_such_evidence_dir"),
            )
            self.assertEqual(code_err, 2)
            self.assertIn("error:", err_err)


if __name__ == "__main__":
    unittest.main()
