from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgv.cli import main
from tgv.efficiency import ScoredSession, aggregate_by_route, score_sessions


def write_log(path: Path, route: str) -> None:
    path.write_text(json.dumps({"route_decision": {"route": route}}) + "\n")


def test_efficiency_grouping() -> None:
    report = aggregate_by_route(
        [
            ScoredSession("local_fast", 0.8, 80, 20),
            ScoredSession("local_fast", 0.6, 30, 20),
            ScoredSession("frontier", 0.9, 90, 10),
        ]
    )
    assert report["local_fast"] == {
        "aggregate_ter": 0.7333,
        "aligned_tokens": 110,
        "waste_tokens": 40,
        "session_count": 2,
    }
    assert report["frontier"]["session_count"] == 1


def test_score_sessions_uses_route_tag(tmp_path: Path) -> None:
    write_log(tmp_path / "a.jsonl", "local_code")
    result = SimpleNamespace(aggregate_ter=0.75, aligned_tokens=75, waste_tokens=25)
    sessions = score_sessions(tmp_path, analyzer=lambda _: result)
    assert sessions == [ScoredSession("local_code", 0.75, 75, 25)]


def test_efficiency_cli_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "tgv.cli.score_sessions",
        lambda _: [ScoredSession("local_fast", 0.8, 8, 2)],
    )
    main(["efficiency", "--format", "json"])
    output = json.loads(capsys.readouterr().out)
    assert output["local_fast"]["aggregate_ter"] == 0.8


def test_efficiency_missing_extra_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    from tgv.efficiency import TERUnavailableError, _ter_analyzer

    original_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("ter_calculator"):
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(TERUnavailableError, match="tgv-agent\\[ter\\]"):
        _ter_analyzer()
