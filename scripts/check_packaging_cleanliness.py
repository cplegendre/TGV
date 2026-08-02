"""Fail if any cache, tool-generated, or scratch artifact is present in the
source tree. Run this before building a distributable archive -- e.g. as
part of `make package` -- rather than inside the pytest suite, since running
pytest (with coverage) and `pip install -e .` regenerate `__pycache__`,
`.pytest_cache`, and `*.egg-info` as a normal side effect of local
development. Checking for those inside the same suite that creates them
would be self-defeating; this script instead gives a single command to run
against a tree you're about to zip and ship.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_FORBIDDEN_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tgv",
}
_FORBIDDEN_FILE_SUFFIXES = (".tmp", ".pyc", ".bak", ".orig", ".swp")


def find_offenders(root: Path = ROOT) -> list[str]:
    offenders: list[str] = []
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        is_forbidden_dir = path.is_dir() and (
            path.name in _FORBIDDEN_DIR_NAMES or path.name.endswith(".egg-info")
        )
        is_forbidden_file = path.is_file() and path.name.endswith(_FORBIDDEN_FILE_SUFFIXES)
        if is_forbidden_dir or is_forbidden_file:
            offenders.append(str(path.relative_to(root)))
    return sorted(offenders)


def main() -> None:
    offenders = find_offenders()
    if offenders:
        print("Packaging cleanliness check failed. Remove before archiving:", file=sys.stderr)
        for item in offenders:
            print(f"  - {item}", file=sys.stderr)
        raise SystemExit(1)
    print("Packaging cleanliness check passed: no cache/temp/generated artifacts found.")


if __name__ == "__main__":
    main()
