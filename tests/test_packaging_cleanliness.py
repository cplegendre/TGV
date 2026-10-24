from __future__ import annotations

from pathlib import Path

from scripts.check_packaging_cleanliness import find_offenders

ROOT = Path(__file__).resolve().parents[1]

# NOTE: __pycache__, .pytest_cache, .mypy_cache, .ruff_cache, and
# *.egg-info are deliberately NOT checked here. Running pytest (with
# coverage) regenerates __pycache__/.pytest_cache mid-run, and `pip
# install -e .` regenerates egg-info, both before this test body ever
# executes -- asserting their absence inside the same dev/test loop that
# creates them would be self-defeating. Those are checked instead by
# scripts/check_packaging_cleanliness.py, run as a pre-packaging step
# (see the Makefile `package` target) against the archive contents, not
# a live working tree that has just been built and tested.

# Suffixes that indicate a scratch/temp file was committed. Nothing in
# the normal dev/build/test loop creates these, so it's safe to assert
# on them directly within the pytest suite.
_FORBIDDEN_FILE_SUFFIXES = (".tmp", ".bak", ".orig", ".swp")


def _iter_paths() -> list[Path]:
    return [p for p in ROOT.rglob("*") if ".git" not in p.parts]


def test_no_temp_or_scratch_files_present() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in _iter_paths()
        if p.is_file() and p.name.endswith(_FORBIDDEN_FILE_SUFFIXES)
    ]
    assert offenders == [], f"Temp/scratch files found in source tree: {offenders}"


def test_packaging_check_ignores_virtualenv_but_flags_scratch_files(tmp_path: Path) -> None:
    cache = tmp_path / ".venv" / "lib" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"")
    (tmp_path / "scratch.tmp").write_text("x")
    assert find_offenders(tmp_path) == ["scratch.tmp"]
