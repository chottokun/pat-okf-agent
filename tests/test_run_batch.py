"""Tests for scripts/run_batch.sh (preflight probe, watchdog, and exit markers)."""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_batch.sh"


def make_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class RunBatchWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.work_dir = self.base / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "prompt.md").write_text(
            "Collect 3 concepts.\n", encoding="utf-8"
        )

    def run_wrapper(self, *args: str, env: dict[str, str] | None = None):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=ROOT,
            env=merged_env,
            text=True,
            capture_output=True,
            timeout=20,
        )

    def test_bash_syntax_check_passes(self):
        res = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr)

    def test_normal_ok_run_with_fake_opencode(self):
        fake_oc = make_executable(
            self.base / "bin" / "fake_opencode",
            'if [[ "$*" == *"Reply with exactly: OK"* ]]; then\n'
            '  echo "OK"\n'
            '  exit 0\n'
            'fi\n'
            'echo "BATCH_WORK_DONE: $*"\n'
            'exit 0\n',
        )
        res = self.run_wrapper(
            "--work-dir", str(self.work_dir),
            "--opencode-bin", str(fake_oc),
            "--poll-interval", "1",
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        log = (self.work_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("PROBE_OK", log)
        self.assertIn("BATCH_STARTED pid=", log)
        self.assertIn("BATCH_WORK_DONE:", log)
        self.assertIn("END_REASON:ok", log)
        self.assertIn("EXIT:0", log)

    def test_probe_fail_on_quota_limit_aborts_before_batch(self):
        marker = self.base / "batch_ran.flag"
        fake_oc = make_executable(
            self.base / "bin" / "fake_opencode",
            'if [[ "$*" == *"Reply with exactly: OK"* ]]; then\n'
            '  echo "Error: Free usage limit reached. OK"\n'
            '  exit 0\n'
            'fi\n'
            f'touch "{marker}"\n'
            'exit 0\n',
        )
        res = self.run_wrapper(
            "--work-dir", str(self.work_dir),
            "--opencode-bin", str(fake_oc),
        )
        self.assertEqual(res.returncode, 42)
        self.assertFalse(marker.exists(), "batch command must not run on probe failure")
        log = (self.work_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("PROBE_FAIL", log)
        self.assertIn("END_REASON:probe_fail", log)
        self.assertIn("EXIT:42", log)
        self.assertNotIn("BATCH_STARTED", log)

    def test_probe_fail_when_ok_marker_missing(self):
        fake_oc = make_executable(
            self.base / "bin" / "fake_opencode",
            'echo "unexpected response without marker"\n'
            'exit 0\n',
        )
        res = self.run_wrapper(
            "--work-dir", str(self.work_dir),
            "--opencode-bin", str(fake_oc),
        )
        self.assertEqual(res.returncode, 42)
        log = (self.work_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("PROBE_FAIL", log)
        self.assertIn("END_REASON:probe_fail", log)
        self.assertIn("EXIT:42", log)

    def test_quota_error_during_batch_kills_hanging_child_and_marks_quota(self):
        probe_ok = make_executable(
            self.base / "bin" / "probe_ok",
            'echo "OK"\nexit 0\n',
        )
        quota_hang = make_executable(
            self.base / "bin" / "quota_hang",
            'echo "step 1: starting"\n'
            'echo "AI_APICallError: Free usage limit reached"\n'
            'sleep 30\n',
        )
        res = self.run_wrapper(
            "--work-dir", str(self.work_dir),
            "--probe-cmd", str(probe_ok),
            "--idle-timeout", "10",
            "--poll-interval", "1",
            "--kill-grace", "1",
            "--", str(quota_hang),
        )
        self.assertEqual(res.returncode, 43)
        log = (self.work_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("PROBE_OK", log)
        self.assertIn("END_REASON:quota", log)
        self.assertIn("EXIT:43", log)

    def test_idle_timeout_kills_silent_hanging_child(self):
        probe_ok = make_executable(
            self.base / "bin" / "probe_ok",
            'echo "OK"\nexit 0\n',
        )
        silent_hang = make_executable(
            self.base / "bin" / "silent_hang",
            'trap "" TERM\n'
            'sleep 30\n',
        )
        res = self.run_wrapper(
            "--work-dir", str(self.work_dir),
            "--probe-cmd", str(probe_ok),
            "--idle-timeout", "2",
            "--poll-interval", "1",
            "--kill-grace", "1",
            "--", str(silent_hang),
        )
        self.assertEqual(res.returncode, 124)
        log = (self.work_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("WATCHDOG_KILL:", log)
        self.assertIn("END_REASON:idle_timeout", log)
        self.assertIn("EXIT:124", log)

    def test_nonzero_command_exit_without_quota_marks_cmd_fail(self):
        probe_ok = make_executable(
            self.base / "bin" / "probe_ok",
            'echo "OK"\nexit 0\n',
        )
        fail_cmd = make_executable(
            self.base / "bin" / "fail_cmd",
            'echo "unexpected failure"\nexit 7\n',
        )
        res = self.run_wrapper(
            "--work-dir", str(self.work_dir),
            "--probe-cmd", str(probe_ok),
            "--poll-interval", "1",
            "--", str(fail_cmd),
        )
        self.assertEqual(res.returncode, 7)
        log = (self.work_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("END_REASON:cmd_fail", log)
        self.assertIn("EXIT:7", log)


if __name__ == "__main__":
    unittest.main()
