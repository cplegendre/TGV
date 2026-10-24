from __future__ import annotations

from pathlib import Path

import yaml

from tgv.cli import main
from tgv.efficiency import ScoredSession, propose_thresholds, waste_ratio


def report(count: int, aligned: int, waste: int) -> dict[str, dict[str, int | float]]:
    return {
        "local_fast": {
            "session_count": count,
            "aligned_tokens": aligned,
            "waste_tokens": waste,
            "aggregate_ter": 0.8,
        }
    }


def test_waste_ratio() -> None:
    assert waste_ratio(report(5, 75, 25)["local_fast"]) == 0.25


def test_calibration_raise_and_lower_boundaries() -> None:
    high = propose_thresholds(
        report(5, 60, 40),
        local_answer_confidence=0.8,
        local_code_confidence=0.7,
    )
    assert high[0].action == "raise" and high[0].proposed_threshold == 0.85
    low = propose_thresholds(
        report(5, 95, 5),
        local_answer_confidence=0.8,
        local_code_confidence=0.7,
    )
    assert low[0].action == "lower" and low[0].proposed_threshold == 0.75


def test_calibration_insufficient_data() -> None:
    proposals = propose_thresholds(
        report(4, 10, 90),
        local_answer_confidence=0.8,
        local_code_confidence=0.7,
    )
    assert proposals[0].action == "insufficient_data"
    assert proposals[0].proposed_threshold == 0.8


def test_calibrate_apply_writes_but_dry_run_does_not(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "tgv.yaml"
    config.write_text("version: 1\nmodels:\n  fast: {provider: ollama, model: qwen3:4b}\n")
    original = config.read_text()
    sessions = [ScoredSession("local_fast", 0.6, 60, 40) for _ in range(5)]
    monkeypatch.setattr("tgv.cli.score_sessions", lambda _: sessions)
    main(["calibrate", "--config", str(config), "--format", "json"])
    assert config.read_text() == original
    main(
        [
            "calibrate",
            "--config",
            str(config),
            "--apply",
            "--accept-legacy-heuristic",
            "--format",
            "json",
        ]
    )
    loaded = yaml.safe_load(config.read_text())
    assert loaded["routing"]["local_answer_confidence"] == 0.87
