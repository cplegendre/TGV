import json

import pytest

from tgv.cli import main


def test_cli_route_offline(sample_repo, capsys) -> None:
    main(["route", "Where is validate_token?", "--repo", str(sample_repo)])
    output = json.loads(capsys.readouterr().out)
    assert output["route"] == "local_fast"


def test_route_explain_includes_policy_details(sample_repo, capsys) -> None:
    main(["route", "Where is authentication configured?", "--repo", str(sample_repo), "--explain"])
    output = json.loads(capsys.readouterr().out)
    assert output["route"] == "local_fast"
    assert output["decision_source"] == "deterministic"
    assert output["thresholds"]["local_answer_confidence"] == 0.82
    assert output["risk_signals"] == []


def test_doctor_reports_operational_summary(tmp_path, capsys) -> None:
    config = tmp_path / "tgv.yaml"
    config.write_text(
        """version: 1
models:
  fast:
    provider: external
    model: tiny
telemetry:
  enabled: false
privacy:
  deny_patterns: [\"**/.env\", \"**/*.pem\"]
""",
        encoding="utf-8",
    )
    main(["doctor", "--config", str(config)])
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "config": "ok",
        "ollama_available": False,
        "openai_compatible_available": False,
        "fast_model": "tiny",
        "telemetry": "disabled",
        "deny_patterns_count": 2,
    }


def test_doctor_reports_config_failure(tmp_path, capsys) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("version: nope\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--config", str(config)])
    assert exc.value.code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["config"] == "fail"
    assert str(config) in output["error"]


def test_cli_reports_repository_error_for_missing_root(tmp_path, capsys) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(SystemExit) as exc:
        main(["inspect", str(missing)])
    assert exc.value.code == 2
    assert "Repository root is not a directory" in capsys.readouterr().err


def test_calibrate_apply_requires_config(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tgv.cli.score_sessions", lambda _path: [])
    with pytest.raises(SystemExit) as exc:
        main(["calibrate", "--sessions", str(tmp_path), "--apply", "--accept-legacy-heuristic"])
    assert exc.value.code == 2
    assert "--apply requires --config" in capsys.readouterr().err


def test_efficiency_text_output_for_empty_report(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tgv.cli.score_sessions", lambda _path: [])
    main(["efficiency", "--sessions", str(tmp_path)])
    assert capsys.readouterr().out.strip() == "route        sessions  TER     aligned  waste"
