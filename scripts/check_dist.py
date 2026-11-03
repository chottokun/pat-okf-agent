#!/usr/bin/env python3
"""Fail if a built wheel/sdist contains secrets or missing CLI files."""
from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path


FORBIDDEN_NAMES = {".secrets", ".git", "__pycache__", "plan"}
REQUIRED_SUFFIX = "pat_okf_agent/wiki/cli.py"


def archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return archive.getnames()
    raise ValueError(f"unsupported archive: {path}")


def check_members(path: Path) -> int:
    members = archive_members(path)
    bad: list[str] = []
    for member in members:
        parts = Path(member).parts
        if FORBIDDEN_NAMES.intersection(parts):
            bad.append(member)
            continue
        if any(parts[index:index + 2] == ("data", "external")
               for index in range(len(parts) - 1)):
            bad.append(member)
            continue
        name = Path(member).name
        if name == ".env" or name.startswith(".env."):
            bad.append(member)
        elif name.endswith(".pyc"):
            bad.append(member)
    if bad:
        raise ValueError(
            f"{path.name} contains forbidden paths:\n  "
            + "\n  ".join(sorted(bad)[:20]))
    if not any(member.endswith(REQUIRED_SUFFIX) for member in members):
        raise ValueError(f"{path.name} is missing {REQUIRED_SUFFIX}")
    return len(members)


def main(argv: list[str]) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "dist")
    if not dist.is_dir():
        print(f"distribution directory not found: {dist}", file=sys.stderr)
        return 1
    archives = sorted(
        path for path in dist.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    wheels = [path for path in archives if path.suffix == ".whl"]
    sdists = [path for path in archives if path.name.endswith(".tar.gz")]
    if not wheels or not sdists:
        print("need at least one wheel and one sdist", file=sys.stderr)
        return 1
    try:
        for path in archives:
            count = check_members(path)
            print(f"checked {path.name}: {count} member(s)")
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"distribution check failed: {exc}", file=sys.stderr)
        return 1
    print(f"distribution check: ok ({len(wheels)} wheel, {len(sdists)} sdist)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
