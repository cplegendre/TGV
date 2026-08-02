from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgv.config import default_config
from tgv.exceptions import CommandExecutionError, ProviderUnavailableError
from tgv.models import HandoffConfig, HandoffMode, RepositoryConfig
from tgv.providers import CommandHandoffProvider
from tgv.repository import Repository
from tgv.router import Router


def test_conflicting_simple_code_high_risk_escalates() -> None:
    decision = Router(default_config().routing).deterministic(
        "Explain which file to patch to drop the database in production"
    )
    assert decision.route.value == "frontier_deep"
    assert "high_risk" in decision.matched_signals
    assert set(decision.score_breakdown) == {
        "local_fast",
        "local_code",
        "frontier_standard",
        "frontier_deep",
    }
    assert decision.alternative_routes


def test_index_invalidation_and_stable_order(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("router token\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("router token\n", encoding="utf-8")
    repo = Repository(tmp_path, RepositoryConfig(), [])
    first = repo.search("router token")
    assert [e.path for e in first] == ["alpha.py", "beta.py"]
    cache = json.loads((tmp_path / ".tgv/index.json").read_text())
    old_size = cache["files"]["alpha.py"]["size"]
    (tmp_path / "alpha.py").write_text("router token token\n", encoding="utf-8")
    second = repo.search("router token")
    cache2 = json.loads((tmp_path / ".tgv/index.json").read_text())
    assert cache2["files"]["alpha.py"]["size"] != old_size
    assert second[0].path == "alpha.py"


def test_sensitive_files_are_denied(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "safe.py").write_text("TOKEN = 'placeholder'", encoding="utf-8")
    config = default_config()
    repo = Repository(tmp_path, config.repository, config.privacy.deny_patterns)
    assert [e.path for e in repo.search("TOKEN")] == ["safe.py"]
    assert repo.omitted_summary().denied >= 1


def test_handoff_config_requires_command() -> None:
    with pytest.raises(ValueError):
        HandoffConfig(mode=HandoffMode.COMMAND)


def test_command_provider_dry_run_expands_paths(tmp_path: Path) -> None:
    md, js = tmp_path / "handoff.md", tmp_path / "bundle.json"
    provider = CommandHandoffProvider(["agent", "--context", "{handoff_markdown}", "{bundle_json}"])
    command = provider.invoke(str(md), str(js), dry_run=True)
    assert command[0] == "agent"
    assert command[-2:] == [str(md.resolve()), str(js.resolve())]


def test_command_provider_reports_missing_executable(tmp_path: Path) -> None:
    provider = CommandHandoffProvider(["definitely-not-a-real-tgv-command"])
    with pytest.raises(ProviderUnavailableError):
        provider.invoke(str(tmp_path / "h.md"), str(tmp_path / "b.json"))


def test_command_provider_reports_failure(tmp_path: Path) -> None:
    provider = CommandHandoffProvider(["python", "-c", "import sys;sys.exit(7)"])
    with pytest.raises(CommandExecutionError, match="exit code 7"):
        provider.invoke(str(tmp_path / "h.md"), str(tmp_path / "b.json"))
