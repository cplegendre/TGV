"""Check that TER's runtime prerequisites stay in the opt-in install contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ter_extra_requests_embeddings_at_verified_release() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras: dict[str, list[str]] = data["project"]["optional-dependencies"]
    assert "ter" in extras
    assert len(extras["ter"]) == 1
    assert extras["ter"][0] == (
        "ter-calculator[embeddings] @ git+https://github.com/cplegendre/TER.git@v3.0.0"
    )
    assert all("ter-calculator" not in dep for dep in data["project"]["dependencies"])


def test_ter_contract_documentation_exists() -> None:
    guide = (ROOT / "docs" / "ter-integration.md").read_text(encoding="utf-8")
    assert "ter analyze /path/to/agent-session.jsonl --format json" in guide
    assert "synthetic" in guide
    assert "3.0.0" in guide


def test_python_support_envelope_is_unchanged() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    classifiers = set(project["classifiers"])
    assert project["requires-python"] == ">=3.11"
    assert {
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    } <= classifiers
