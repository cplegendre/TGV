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

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PRUNED_DIR_NAMES = {".git", ".venv", "venv"}

_FORBIDDEN_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tgv",
}
_FORBIDDEN_FILE_SUFFIXES = (".tmp", ".pyc", ".bak", ".orig", ".swp")


def _iter_candidates(root: Path) -> list[Path]:
    """Return prospective archive members, excluding local virtualenvs.

    In a git checkout, honour ``.gitignore`` so locally generated caches and
    runtime state are not mistaken for archive contents. Fall back to a full
    walk (minus VCS/virtualenv directories) for extracted archives.
    """
    if (root / ".git").exists():
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            pass
        else:
            return [root / name for name in completed.stdout.decode().split("\0") if name]
    return [
        path
        for path in root.rglob("*")
        if not any(part in _PRUNED_DIR_NAMES for part in path.relative_to(root).parts)
    ]


def find_offenders(root: Path = ROOT) -> list[str]:
    offenders: list[str] = []
    for path in _iter_candidates(root):
        is_forbidden_dir = path.is_dir() and (
            path.name in _FORBIDDEN_DIR_NAMES or path.name.endswith(".egg-info")
        )
        is_forbidden_file = path.is_file() and path.name.endswith(_FORBIDDEN_FILE_SUFFIXES)
        if is_forbidden_dir or is_forbidden_file:
            offenders.append(str(path.relative_to(root)))
    return sorted(offenders)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="tree to inspect (defaults to the repository root)",
    )
    args = parser.parse_args()
    offenders = find_offenders(args.root)
    if offenders:
        print("Packaging cleanliness check failed. Remove before archiving:", file=sys.stderr)
        for item in offenders:
            print(f"  - {item}", file=sys.stderr)
        raise SystemExit(1)
    print("Packaging cleanliness check passed: no cache/temp/generated artifacts found.")


if __name__ == "__main__":
    main()
