#!/usr/bin/env python3
"""Public-release guard for tracked repository content.

This is intentionally conservative: it only fails on high-confidence secret
material and on internal absolute paths. It never prints the matching value.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

FORBIDDEN_PATHS = (
    re.compile(r"(^|/)\.secrets(/|$)"),
    re.compile(r"^data/external(/|$)"),
    re.compile(r"^plan(/|$)"),
    re.compile(r"(^|/)BUNDLE_README\.md$"),
    re.compile(r"(^|/)\.env(?:\.|$)"),
    re.compile(r"(^|/)\.venv(/|$)"),
    re.compile(r"(^|/)__pycache__(/|$)"),
    re.compile(r"(^|/)dist(/|$)"),
    re.compile(r"\.(?:pem|key|p12|pfx|sqlite3?|db|onnx|pt|pth|bin)$"),
)

SECRET_PATTERNS = (
    ("private-key",
     re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token",
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|"
                r"github_pat_[A-Za-z0-9_]{30,})\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)

GENERIC_ASSIGNMENT = re.compile(
    r"(?i)\b(?:consumer_secret|consumer_key|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|password)\b\s*[:=]\s*[\"']([^\"']{8,})[\"']")
PLACEHOLDER = re.compile(
    r"(?i)(?:your|change[-_ ]?me|example|dummy|fake|test|redacted|"
    r"placeholder|\.\.\.|<[^>]+>|x{6,})")
INTERNAL_PATH = re.compile(r"/(?:workspace|root|home)/")
GENERIC_SKIP = ("tests/", SELF)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def main() -> int:
    findings: list[str] = []
    for rel in tracked_files():
        if any(pattern.search(rel) for pattern in FORBIDDEN_PATHS):
            findings.append(f"{rel}: forbidden path")
            continue
        if rel == SELF:
            continue
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append(f"{rel}: unreadable ({exc})")
            continue
        if b"\0" in data[:4096] or len(data) > 2_000_000:
            continue
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(f"{rel}:{line_no}: {name}")
            if INTERNAL_PATH.search(line):
                findings.append(f"{rel}:{line_no}: internal absolute path")
            if not rel.startswith(GENERIC_SKIP):
                match = GENERIC_ASSIGNMENT.search(line)
                if match and not PLACEHOLDER.search(match.group(1)):
                    findings.append(
                        f"{rel}:{line_no}: credential-like assignment")
    if findings:
        print("public release guard: FAILED", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print("public release guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
