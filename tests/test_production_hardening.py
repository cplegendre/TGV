from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from tgv.cli import main
from tgv.compiler import ContextCompiler
from tgv.config import default_config
from tgv.efficiency import (
    ScoredSession,
    _route_from_log,
    aggregate_by_route,
    propose_thresholds,
    score_sessions,
    waste_ratio,
)
from tgv.exceptions import (
    CommandExecutionError,
    ConfigurationError,
    ProviderUnavailableError,
    RepositoryAccessError,
    ValidationFailure,
)
from tgv.models import (
    ContextBundle,
    HandoffConfig,
    HandoffMode,
    ModelRoleConfig,
    Route,
    RouteDecision,
)
from tgv.providers import CommandHandoffProvider, OllamaProvider
from tgv.repository import Repository
from tgv.service import TGVService


def test_repository_rejects_absolute_and_parent_paths(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    repo = Repository(tmp_path, default_config().repository, [])
    for value in ("../safe.txt", "/etc/passwd", "sub/../../safe.txt", ""):
        with pytest.raises(RepositoryAccessError, match="Path escapes"):
            repo.read_text(value)


def test_repository_rejects_internal_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP SECRET", encoding="utf-8")
    link = root / "nested"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    repo = Repository(root, default_config().repository, [])
    with pytest.raises(RepositoryAccessError, match="escapes repository root"):
        repo.read_text("nested/secret.txt")
    assert "TOP SECRET" not in json.dumps([item.model_dump() for item in repo.search("secret")])


def test_repository_index_path_cannot_escape(tmp_path: Path) -> None:
    config = default_config().repository.model_copy(update={"index_path": Path("../index.json")})
    repo = Repository(tmp_path, config, [])
    assert repo.build_index() == {}
    assert not (tmp_path.parent / "index.json").exists()


def test_repository_cache_invalid_and_scan_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("needle needle", encoding="utf-8")
    config = default_config().repository
    repo = Repository(tmp_path, config, [])
    (tmp_path / config.index_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / config.index_path).write_text('{"version": 99, "files": []}', encoding="utf-8")
    assert repo.build_index()["a.py"]["tokens"]["needle"] == 2
    monkeypatch.setattr(repo, "build_index", Mock(side_effect=ValueError("bad cache")))
    assert repo.search("needle")[0].path == "a.py"


def test_repository_handles_stat_and_read_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.txt"
    path.write_text("needle", encoding="utf-8")
    repo = Repository(tmp_path, default_config().repository, [])
    original_stat = Path.stat

    def broken_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self == path:
            raise OSError("denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", broken_stat)
    assert repo.list_files() == []


def test_repository_zero_limit_and_empty_query(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle", encoding="utf-8")
    repo = Repository(tmp_path, default_config().repository, [])
    assert repo.list_files(0) == []
    assert repo.search("!!") == []
    assert repo.search("needle", 0) == []


def test_efficiency_error_and_edge_branches(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"other": 1}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No route_decision"):
        _route_from_log(bad)
    empty = aggregate_by_route([ScoredSession("local_fast", 0.2, 0, 0)])
    assert empty["local_fast"]["aggregate_ter"] == 1.0
    assert waste_ratio({"aligned_tokens": 0, "waste_tokens": 0}) == 0.0
    report = {
        "local_fast": {"aligned_tokens": 5, "waste_tokens": 95, "session_count": 5},
        "local_code": {"aligned_tokens": 95, "waste_tokens": 5, "session_count": 5},
    }
    proposals = propose_thresholds(report, local_answer_confidence=0.98, local_code_confidence=0.02)
    assert proposals[0].proposed_threshold == 1.0
    assert proposals[1].proposed_threshold == 0.0


def test_score_sessions_default_analyzer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_text(json.dumps({"route_decision": {"route": "frontier_deep"}}) + "\n")
    analyzer = Mock(
        return_value=SimpleNamespace(aggregate_ter=0.5, aligned_tokens=1, waste_tokens=1)
    )
    monkeypatch.setattr("tgv.efficiency._ter_analyzer", lambda: analyzer)
    assert score_sessions(tmp_path)[0].route == "frontier_deep"


def test_command_provider_validation_and_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigurationError):
        CommandHandoffProvider([])
    provider = CommandHandoffProvider(["missing-command"])
    with pytest.raises(ProviderUnavailableError):
        provider.invoke(str(tmp_path / "h.md"), str(tmp_path / "b.json"))

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(CommandExecutionError, match="timed out"):
        CommandHandoffProvider([sys.executable], 0.01).invoke("h", "j")

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 3, "", "bad")
    )
    with pytest.raises(CommandExecutionError, match="exit code 3"):
        CommandHandoffProvider([sys.executable]).invoke("h", "j")


def test_handoff_config_rejects_nul() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        HandoffConfig(command=["bad\x00arg"])


def test_ollama_provider_validation_available_and_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigurationError, match="Expected ollama"):
        OllamaProvider(ModelRoleConfig(provider="other", model="x", base_url="http://x"))
    client = Mock()
    client.get.return_value = SimpleNamespace(is_success=True)
    provider = OllamaProvider(
        ModelRoleConfig(provider="ollama", model="x", base_url="http://x"), client=client
    )
    assert provider.available() is True
    client.get.side_effect = __import__("httpx").ConnectError("offline")
    assert provider.available() is False


def test_compiler_without_repository_summary() -> None:
    decision = RouteDecision(route=Route.LOCAL_FAST, confidence=1, rationale="x")
    bundle = ContextBundle(
        request="x", repository_root=".", decision=decision, evidence=[], handoff_markdown=""
    )
    text = ContextCompiler._handoff(bundle)
    assert "No matching repository evidence" in text


def test_service_command_handoff_and_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = default_config()
    config.handoff.mode = HandoffMode.COMMAND
    config.handoff.command = [sys.executable, "-c", "pass"]
    repo = Repository(tmp_path, config.repository, config.privacy.deny_patterns)
    service = TGVService(config, repo)
    bundle = service.prepare("Where is x?")
    command = service.handoff(bundle, tmp_path / "out", dry_run=True)
    assert command and command[0] == sys.executable
    service.close()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ConfigurationError("bad"), 2),
        (ProviderUnavailableError("bad"), 4),
        (CommandExecutionError("bad"), 5),
        (ValidationFailure("bad"), 6),
    ],
)
def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, code: int
) -> None:
    monkeypatch.setattr("tgv.cli.TGVService.create", Mock(side_effect=error))
    with pytest.raises(SystemExit) as exc:
        main(["inspect", str(tmp_path)])
    assert exc.value.code == code


def test_cli_calibrate_apply_and_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "tgv.yaml"
    config_path.write_text("version: 1\nmodels: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "tgv.cli.score_sessions",
        lambda _: [ScoredSession("local_fast", 0.5, 90, 10)] * 5,
    )
    main(["calibrate", "--config", str(config_path), "--apply"])
    assert "local_fast:" in capsys.readouterr().out
    assert "local_answer_confidence" in config_path.read_text(encoding="utf-8")


def test_cli_efficiency_nonempty_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("tgv.cli.score_sessions", lambda _: [ScoredSession("frontier", 0.9, 9, 1)])
    main(["efficiency"])
    assert "frontier" in capsys.readouterr().out


def test_optional_ter_analyzer_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import types

    module = types.ModuleType("ter_calculator.analyze_pipeline")
    module.default_analyze_args = lambda path: {"path": path}  # type: ignore[attr-defined]
    module.analyze_session = lambda args: SimpleNamespace(
        aggregate_ter=1, aligned_tokens=1, waste_tokens=0
    )  # type: ignore[attr-defined]
    package = types.ModuleType("ter_calculator")
    monkeypatch.setitem(sys.modules, "ter_calculator", package)
    monkeypatch.setitem(sys.modules, "ter_calculator.analyze_pipeline", module)
    from tgv.efficiency import _ter_analyzer

    result = _ter_analyzer()(tmp_path / "x.jsonl")
    assert result.aligned_tokens == 1


def test_calibration_hold_branch() -> None:
    report = {"local_fast": {"aligned_tokens": 80, "waste_tokens": 20, "session_count": 5}}
    proposals = propose_thresholds(report, local_answer_confidence=0.8, local_code_confidence=0.7)
    assert proposals[0].action == "hold"


def test_ollama_rejects_non_string_content() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": 3}})

    provider = OllamaProvider(
        ModelRoleConfig(provider="ollama", model="x", base_url="http://x", maximum_retries=0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    from tgv.exceptions import ModelProviderError

    with pytest.raises(ModelProviderError):
        provider.route("x")


def test_command_provider_success(tmp_path: Path) -> None:
    provider = CommandHandoffProvider([sys.executable, "-c", "pass"])
    result = provider.invoke(str(tmp_path / "h.md"), str(tmp_path / "b.json"))
    assert result[0] == sys.executable


def test_repository_read_oserror_and_search_excerpt_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.txt"
    path.write_text("needle", encoding="utf-8")
    repo = Repository(tmp_path, default_config().repository, [])
    original = Path.read_bytes

    def broken(self: Path) -> bytes:
        if self == path:
            raise OSError("blocked")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", broken)
    with pytest.raises(RepositoryAccessError, match="Cannot read"):
        repo.read_text("a.txt")
    assert repo.search("needle") == []


def test_repository_enumeration_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Repository(tmp_path, default_config().repository, [])
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(RepositoryAccessError, match="enumerate"):
        repo.list_files()
