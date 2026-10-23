from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml

from tgv import calibration
from tgv.bench.observed import ObservedBenchmarkError
from tgv.calibration import (
    EvidenceCalibrationError,
    apply_evidence_calibration,
    evidence_calibration,
    render_evidence_calibration,
)
from tgv.cli import main
from tgv.config import default_config, load_config
from tgv.exceptions import TGVError
from tgv.intelligence import TaskProfiler
from tgv.models import CapabilityTier, Route, RouteDecision, RoutingConfig
from tgv.outcomes import OutcomeLedger
from tgv.router import Router
from tgv.service import TGVService
from tgv.session_linking import link_session_to_task

SECRET_TEXT = "SECRET-PR07-SESSION-CONTENT"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _ledger(repo: Path) -> OutcomeLedger:
    return OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")


def _config_path(
    tmp_path: Path,
    *,
    local_code_confidence: float = 0.68,
    local_answer_confidence: float = 0.82,
) -> Path:
    config = default_config()
    config.routing.local_code_confidence = local_code_confidence
    config.routing.local_answer_confidence = local_answer_confidence
    path = tmp_path / "tgv.yaml"
    path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return path


def _session(path: Path, session_id: str) -> Path:
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "message": {"role": "user", "content": SECRET_TEXT},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": "Completed the task."},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _task(
    repo: Path,
    request: str,
    *,
    route: Route = Route.LOCAL_CODE,
    decision_source: str = "model",
    tier: CapabilityTier = CapabilityTier.STANDARD,
    model: str = "local-code",
) -> str:
    ledger = _ledger(repo)
    profile = TaskProfiler(default_config().intelligence).profile(request)
    task_id = ledger.create_pending(request, repo, profile)
    ledger.record_routing(
        task_id,
        revised_profile=profile,
        confidence=0.9,
        model=model,
        capability_tier=tier,
        reasons=["test"],
        files=[],
        symbols=[],
        context_token_estimate=10,
        route=route,
        decision_source=decision_source,
    )
    return task_id


def _link(repo: Path, task_id: str, session_id: str) -> Any:
    session = _session(repo.parent / f"{session_id}.jsonl", session_id)
    return link_session_to_task(
        _ledger(repo),
        task_id,
        repository_root=repo,
        session_path=session,
        agent="claude-code",
    )


def _measure(
    repo: Path,
    result: Any,
    *,
    aligned: int = 60,
    waste: int = 40,
    analyzer_version: str = "3.0.0",
) -> None:
    _ledger(repo).record_ter_measurement(
        result.attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version=analyzer_version,
        metrics={
            "aggregate_ter": aligned / (aligned + waste) if aligned + waste else 1.0,
            "aligned_tokens": aligned,
            "waste_tokens": waste,
            "total_tokens": aligned + waste,
        },
        session_link_id=result.link.session_link_id,
    )


def _complete(repo: Path, task_id: str, *, success: bool | None = True) -> None:
    ledger = _ledger(repo)
    ledger.record_execution(task_id, status="completed")
    if success is not None:
        ledger.record_validation(
            task_id,
            tests_passed=success,
            static_checks_passed=None,
        )


def _evidence(
    repo: Path,
    count: int,
    *,
    success: bool | None = True,
    aligned: int = 60,
    waste: int = 40,
    route: Route = Route.LOCAL_CODE,
    decision_source: str = "model",
    analyzer_version: str = "3.0.0",
) -> None:
    for index in range(count):
        task_id = _task(
            repo,
            f"fix helper bug {index}",
            route=route,
            decision_source=decision_source,
        )
        session_id = hashlib.sha256(task_id.encode()).hexdigest()
        result = _link(repo, task_id, session_id)
        _measure(
            repo,
            result,
            aligned=aligned,
            waste=waste,
            analyzer_version=analyzer_version,
        )
        _complete(repo, task_id, success=success)


def _report(repo: Path, config_path: Path) -> dict[str, Any]:
    return evidence_calibration(
        OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3", read_only=True),
        load_config(config_path),
        repo,
    )


def _application_report(repo: Path, config_path: Path) -> dict[str, Any]:
    """Exercise the dormant writer with an explicit hypothetical eligible proposal.

    This is not observational authorization: the production engine never emits it.
    """
    report = _report(repo, config_path)
    proposal = _proposal(report, "local_code")
    proposal.update(
        status="eligible", application_permitted=True, proposed_threshold=0.73, direction="raise"
    )
    return report


def _proposal(report: dict[str, Any], target: str) -> dict[str, Any]:
    return next(item for item in report["proposals"] if item["target"] == target)


def test_missing_ledger_returns_insufficient_evidence_without_creation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    config_path = _config_path(tmp_path)
    report = _report(repo, config_path)

    assert report["coverage"]["total_task_count"] == 0
    assert all(proposal["status"] == "insufficient-evidence" for proposal in report["proposals"])
    assert not (repo / ".tgv").exists()


def test_empty_ledger_returns_insufficient_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ledger(repo).query()
    config_path = _config_path(tmp_path)

    report = _report(repo, config_path)

    assert report["coverage"]["total_task_count"] == 0
    assert _proposal(report, "local_code")["verified_outcome_count"] == 0


def test_insufficient_verified_outcomes_blocks_calibration(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 19)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["status"] == "insufficient-evidence"
    assert proposal["application_permitted"] is False
    assert proposal["proposed_threshold"] == 0.68


def test_missing_ter_measurements_are_not_zero_efficiency(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(40):
        task_id = _task(repo, f"fix helper bug {index}")
        _complete(repo, task_id)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_outcome_count"] == 40
    assert proposal["efficiency_measurement_count"] == 0
    assert proposal["aggregate_ter"] is None
    assert proposal["waste_ratio"] is None
    assert proposal["status"] == "efficiency-evidence-missing"


def test_unverified_completed_tasks_are_not_successes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=None)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_outcome_count"] == 0
    assert proposal["verified_success_count"] == 0
    assert proposal["unverified_task_count"] == 40
    assert proposal["status"] == "insufficient-evidence"


def test_verified_failures_and_low_success_fail_correctness_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 20, success=False)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_failure_count"] == 20
    assert proposal["verified_success_rate"] == 0.0
    assert proposal["correctness_gate"]["passed"] is False
    assert proposal["status"] == "correctness-gate-failed"


def test_mixed_verified_success_and_failure_fails_correctness_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 10, success=True)
    _evidence(repo, 10, success=False)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_success_count"] == 10
    assert proposal["verified_failure_count"] == 10
    assert proposal["verified_success_rate"] == 0.5
    assert proposal["status"] == "correctness-gate-failed"


def test_insufficient_ter_measurements_block_calibration(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(40):
        task_id = _task(repo, f"fix helper bug {index}")
        if index < 19:
            result = _link(
                repo,
                task_id,
                hashlib.sha256(task_id.encode()).hexdigest(),
            )
            _measure(repo, result)
        _complete(repo, task_id)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_outcome_count"] == 40
    assert proposal["efficiency_measurement_count"] == 19
    assert proposal["paired_task_count"] == 19
    assert proposal["status"] == "efficiency-evidence-missing"


def test_high_point_estimate_with_weak_wilson_bound_blocks_change(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 20, success=True)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_success_rate"] == 1.0
    assert proposal["correctness_gate"]["lower_confidence_bound"] < 0.90
    assert proposal["status"] == "correctness-gate-failed"
    assert proposal["application_permitted"] is False


def test_strong_success_and_high_waste_remain_diagnostics_only(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_outcome_count"] == 40
    assert proposal["efficiency_measurement_count"] == 40
    assert proposal["paired_task_count"] == 40
    assert proposal["correctness_gate"]["passed"] is True
    assert proposal["waste_ratio"] == 0.4
    assert proposal["status"] == "no-change"
    assert proposal["direction"] == "none"
    assert proposal["proposed_threshold"] == 0.68
    assert proposal["application_permitted"] is False
    assert proposal["verification_coverage"] == 1.0
    assert "effect of rejecting model suggestions" in proposal["rationale"]


def test_low_waste_does_not_justify_threshold_lowering(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=90, waste=10)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["correctness_gate"]["passed"] is True
    assert proposal["waste_ratio"] == 0.1
    assert proposal["status"] == "no-change"
    assert proposal["direction"] == "none"
    assert proposal["proposed_threshold"] == 0.68


def test_zero_token_sessions_retain_ter_semantics(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=0, waste=0)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["aggregate_ter"] == 1.0
    assert proposal["waste_ratio"] == 0.0
    assert proposal["status"] == "no-change"


def test_mixed_analyzer_versions_select_one_comparable_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 20, analyzer_version="3.0.0")
    _evidence(repo, 20, analyzer_version="3.0.1")
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert report["selected_analyzer_version"] == "3.0.0"
    assert proposal["efficiency_measurement_count"] == 20
    assert proposal["paired_task_count"] == 20


def test_selected_unsupported_analyzer_version_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 21, analyzer_version="3.0.1")
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert report["selected_analyzer_version"] == "3.0.1"
    assert proposal["status"] == "unsupported-analyzer"
    assert proposal["application_permitted"] is False


def test_missing_and_invalid_route_and_source_are_excluded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 2)

    historical_task = _task(repo, "historical route", decision_source="model")
    _complete(repo, historical_task)
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE outcomes SET metadata = json_remove(metadata, '$.authoritative_route') "
            "WHERE task_id = ?",
            (historical_task,),
        )

    invalid_route_task = _task(repo, "invalid route", decision_source="model")
    _complete(repo, invalid_route_task)
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE outcomes SET metadata = json_set(metadata, '$.authoritative_route', 'bogus') "
            "WHERE task_id = ?",
            (invalid_route_task,),
        )

    invalid_source_task = _task(repo, "invalid source", decision_source="bogus")
    _complete(repo, invalid_source_task)

    report = _report(repo, _config_path(tmp_path))

    assert report["coverage"]["eligible_task_count"] == 2
    assert report["coverage"]["excluded_task_count"] == 3
    assert report["coverage"]["route_counts"]["unavailable"] == 1
    assert report["coverage"]["route_counts"]["invalid-route"] == 1
    assert report["coverage"]["routing_decision_source_counts"]["invalid-decision-source"] == 1


def test_scoreboard_and_deterministic_decisions_are_excluded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 1, decision_source="scoreboard")
    _evidence(repo, 1, decision_source="deterministic")
    report = _report(repo, _config_path(tmp_path))

    assert report["coverage"]["eligible_task_count"] == 0
    assert report["coverage"]["routing_decision_source_counts"]["scoreboard"] == 1
    assert report["coverage"]["routing_decision_source_counts"]["deterministic"] == 1


def test_multiple_attempts_deduplicate_task_outcomes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first_task = _task(repo, "retry task")
    first_result = _link(repo, first_task, f"{1:032x}")
    second_result = _link(repo, first_task, f"{2:032x}")
    _measure(repo, first_result)
    _measure(repo, second_result)
    _complete(repo, first_task)
    for index in range(3, 21):
        task_id = _task(repo, f"fix helper bug {index}")
        result = _link(repo, task_id, f"{index:032x}")
        _measure(repo, result)
        _complete(repo, task_id)

    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")

    assert proposal["verified_outcome_count"] == 19
    assert proposal["efficiency_measurement_count"] == 20
    assert proposal["paired_task_count"] == 19
    assert proposal["status"] == "insufficient-evidence"


def test_wilson_interval_boundaries() -> None:
    assert calibration._wilson_interval(0, 20) == pytest.approx([0.0, 0.16112515805281938])
    assert calibration._wilson_interval(20, 20) == pytest.approx([0.8388748419471806, 1.0])
    assert calibration._wilson_interval(0, 0) is None


def test_maximum_threshold_boundary_returns_no_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path, local_code_confidence=1.0)
    report = _report(repo, config_path)
    proposal = _proposal(report, "local_code")

    assert proposal["status"] == "no-change"
    assert proposal["current_threshold"] == 1.0
    assert proposal["proposed_threshold"] == 1.0


def test_deterministic_json_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 21)
    config_path = _config_path(tmp_path)

    first = json.dumps(_report(repo, config_path), sort_keys=True)
    second = json.dumps(_report(repo, config_path), sort_keys=True)

    assert first == second


def test_service_prepare_persists_routing_decision_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    request = " ".join(
        f"Fix bug number {number} in helper function exactly." for number in range(25)
    )
    with closing(TGVService.create(repo)) as service:
        bundle = service.prepare(request)
        task_id = next(
            assumption.removeprefix("outcome_task_id=")
            for assumption in bundle.assumptions
            if assumption.startswith("outcome_task_id=")
        )
        task = service.outcomes.query(repository_id=service.outcomes.repository_id(repo))[0]

    assert task.task_id == task_id
    assert task.metadata["authoritative_route"] == bundle.decision.route.value
    assert task.metadata["routing_decision_source"] == "deterministic"


def test_cli_evidence_json_and_text_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    config_path = _config_path(tmp_path)

    main(["calibrate", "--evidence", "--repo", str(repo), "--config", str(config_path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "evidence-ledger"

    main(["calibrate", "--evidence", "--repo", str(repo), "--config", str(config_path)])
    text = capsys.readouterr().out
    assert "source: evidence-ledger" in text
    assert "local_code: status=insufficient-evidence" in text


def test_cli_rejects_incompatible_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["calibrate", "--evidence", "--repo", str(repo), "--sessions", "sessions"])
    assert exc_info.value.code == 2
    assert "--evidence does not support --sessions" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["calibrate", "--json"])
    with pytest.raises(SystemExit):
        main(["calibrate", "--evidence", "--repo", str(repo), "--json", "--format", "text"])
    assert "--json cannot be combined" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["calibrate", "--evidence", "--repo", str(repo), "--step-size", "0.01"])
    assert "legacy threshold tuning options" in capsys.readouterr().err


def test_cli_legacy_calibration_remains_compatible(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    config_path = _config_path(tmp_path)
    monkeypatch.setattr("tgv.cli.score_sessions", lambda sessions: [])

    main(["calibrate", "--sessions", str(sessions), "--config", str(config_path)])
    text = capsys.readouterr().out
    assert "local_fast: 0 sessions is below the minimum" in text

    main(
        [
            "calibrate",
            "--sessions",
            str(sessions),
            "--config",
            str(config_path),
            "--format",
            "json",
        ]
    )
    proposals = json.loads(capsys.readouterr().out)
    assert [proposal["route"] for proposal in proposals] == ["local_fast", "local_code"]


def test_default_evidence_mode_is_read_only_and_private(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 21)
    config_path = _config_path(tmp_path)
    original_config = config_path.read_bytes()
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        before = "\n".join(connection.iterdump())

    report = _report(repo, config_path)
    serialized = json.dumps(report, sort_keys=True)
    with sqlite3.connect(database_path) as connection:
        after = "\n".join(connection.iterdump())

    assert before == after
    assert config_path.read_bytes() == original_config
    assert SECRET_TEXT not in serialized
    assert str(tmp_path) not in serialized


def test_malformed_ledger_schema_is_rejected_without_modification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 1)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_version SET version = 'bogus'")
        before = "\n".join(connection.iterdump())

    with pytest.raises(TGVError, match="invalid schema version value"):
        _report(repo, _config_path(tmp_path))

    with sqlite3.connect(database_path) as connection:
        after = "\n".join(connection.iterdump())
    assert before == after


def test_missing_decision_source_and_writable_ledger_access(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo, "missing source")
    _complete(repo, task_id)
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE outcomes SET metadata = "
            "json_remove(metadata, '$.routing_decision_source') WHERE task_id = ?",
            (task_id,),
        )

    report = evidence_calibration(
        _ledger(repo),
        load_config(_config_path(tmp_path)),
        repo,
    )

    assert report["coverage"]["routing_decision_source_counts"]["unavailable"] == 1
    assert report["coverage"]["eligible_task_count"] == 0


def test_observation_loader_errors_are_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 1)

    def fail(ledger: object, repository_root: Path) -> object:
        raise ObservedBenchmarkError("unsafe association")

    monkeypatch.setattr(calibration, "_load_observations", fail)
    with pytest.raises(EvidenceCalibrationError, match="Cannot calibrate"):
        _report(repo, _config_path(tmp_path))


def test_apply_validation_rejects_unreadable_and_inconsistent_configuration(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path)
    report = _application_report(repo, config_path)
    original_bytes = config_path.read_bytes()
    original_config = load_config(config_path)

    with pytest.raises(EvidenceCalibrationError, match="Cannot read configuration"):
        apply_evidence_calibration(
            report,
            tmp_path / "missing.yaml",
            original_config,
            original_bytes,
        )

    changed_original = copy.deepcopy(original_config)
    changed_original.routing.maximum_tool_rounds = 7
    with pytest.raises(EvidenceCalibrationError, match="Configuration changed"):
        apply_evidence_calibration(
            report,
            config_path,
            changed_original,
            original_bytes,
        )

    unsupported = copy.deepcopy(report)
    unsupported["proposals"][1]["threshold_name"] = "unsupported_threshold"
    with pytest.raises(EvidenceCalibrationError, match="Unsupported calibration"):
        apply_evidence_calibration(
            unsupported,
            config_path,
            original_config,
            original_bytes,
        )

    invalid_range = copy.deepcopy(report)
    invalid_range["proposals"][1]["proposed_threshold"] = 1.1
    with pytest.raises(EvidenceCalibrationError, match="Invalid proposed threshold"):
        apply_evidence_calibration(
            invalid_range,
            config_path,
            original_config,
            original_bytes,
        )

    stale_threshold = copy.deepcopy(report)
    stale_threshold["proposals"][1]["current_threshold"] = 0.5
    with pytest.raises(EvidenceCalibrationError, match="Configuration threshold changed"):
        apply_evidence_calibration(
            stale_threshold,
            config_path,
            original_config,
            original_bytes,
        )


def test_render_evidence_calibration_reports_applied_thresholds(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path)
    report = _application_report(repo, config_path)

    applied = apply_evidence_calibration(
        report,
        config_path,
        load_config(config_path),
        config_path.read_bytes(),
    )

    assert "applied thresholds: local_code_confidence=0.73" in render_evidence_calibration(applied)


def test_apply_eligible_proposal_updates_only_target_threshold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path)
    report = _application_report(repo, config_path)

    applied = apply_evidence_calibration(
        report,
        config_path,
        load_config(config_path),
        config_path.read_bytes(),
    )
    updated = load_config(config_path)

    assert applied["applied"] == {
        "requested": True,
        "changed": True,
        "thresholds": {"local_code_confidence": 0.73},
    }
    assert updated.routing.local_code_confidence == 0.73
    assert updated.routing.local_answer_confidence == 0.82
    assert updated.models == default_config().models


def test_apply_insufficient_evidence_does_not_write(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 19)
    config_path = _config_path(tmp_path)
    original = config_path.read_bytes()
    report = _report(repo, config_path)

    applied = apply_evidence_calibration(
        report,
        config_path,
        load_config(config_path),
        original,
    )

    assert applied["applied"]["changed"] is False
    assert config_path.read_bytes() == original


def test_apply_failed_correctness_gate_does_not_write(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 20, success=True)
    config_path = _config_path(tmp_path)
    original = config_path.read_bytes()
    report = _report(repo, config_path)

    applied = apply_evidence_calibration(
        report,
        config_path,
        load_config(config_path),
        original,
    )

    assert _proposal(report, "local_code")["status"] == "correctness-gate-failed"
    assert applied["applied"]["changed"] is False
    assert config_path.read_bytes() == original


def test_atomic_apply_failure_leaves_configuration_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path)
    original = config_path.read_bytes()
    report = _application_report(repo, config_path)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(calibration.os, "replace", fail_replace)
    with pytest.raises(EvidenceCalibrationError, match="atomically update"):
        apply_evidence_calibration(
            report,
            config_path,
            load_config(config_path),
            original,
        )

    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob(f".{config_path.name}.*.tmp"))


def test_stale_configuration_blocks_apply(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path)
    original = config_path.read_bytes()
    report = _application_report(repo, config_path)
    stale_config = default_config()
    stale_config.routing.maximum_tool_rounds = 7
    config_path.write_text(
        yaml.safe_dump(stale_config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceCalibrationError, match="Configuration changed"):
        apply_evidence_calibration(
            report,
            config_path,
            load_config(config_path),
            original,
        )


def test_invalid_configuration_blocks_apply(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path)
    report = _application_report(repo, config_path)
    original = b"routing: ["
    config_path.write_bytes(original)

    with pytest.raises(EvidenceCalibrationError, match="Invalid configuration"):
        apply_evidence_calibration(
            report,
            config_path,
            default_config(),
            original,
        )


def test_cli_apply_high_waste_does_not_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40, success=True, aligned=60, waste=40)
    config_path = _config_path(tmp_path)

    main(
        [
            "calibrate",
            "--evidence",
            "--repo",
            str(repo),
            "--config",
            str(config_path),
            "--apply",
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert report["applied"] == {"requested": True, "changed": False, "thresholds": {}}
    assert load_config(config_path).routing.local_code_confidence == 0.68
    assert not list(config_path.parent.glob(".tgv.yaml.*"))


def test_render_evidence_calibration_is_bounded_and_actionable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 21)
    report = _report(repo, _config_path(tmp_path))

    text = render_evidence_calibration(report)

    assert "status=insufficient-evidence" in text
    assert "applied thresholds: none" in text
    assert SECRET_TEXT not in text


@pytest.mark.parametrize(
    ("task_request", "fallback"),
    [("Where is the helper function?", Route.LOCAL_FAST), ("Fix helper bug", Route.LOCAL_CODE)],
)
def test_former_threshold_raise_can_retain_or_reduce_route_capability(
    task_request: str,
    fallback: Route,
) -> None:
    class Provider:
        def route(self, request: str) -> RouteDecision:
            return RouteDecision(route=Route.LOCAL_CODE, confidence=0.70, rationale="model")

    before = Router(RoutingConfig(local_code_confidence=0.68), Provider()).route(task_request)
    after = Router(RoutingConfig(local_code_confidence=0.73), Provider()).route(task_request)
    assert (before.route, before.decision_source) == (Route.LOCAL_CODE, "model")
    assert (after.route, after.decision_source) == (fallback, "deterministic")
    assert after == Router(RoutingConfig()).deterministic(task_request)


def test_wilson_gate_uses_full_precision_with_real_outcomes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 120)
    _evidence(repo, 6, success=False)
    proposal = _proposal(_report(repo, _config_path(tmp_path)), "local_code")
    interval = calibration._wilson_interval(120, 126)
    assert interval is not None
    assert interval[0] == pytest.approx(0.8999983205231048)
    assert interval[0] < 0.9
    assert proposal["verified_success_confidence_interval_95"][0] == 0.9
    assert proposal["correctness_gate"]["lower_confidence_bound"] < 0.9
    assert proposal["correctness_gate"]["comparison_uses_unrounded_bound"] is True
    assert proposal["correctness_gate"]["passed"] is False
    assert proposal["status"] == "correctness-gate-failed"
    assert proposal["application_permitted"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_tokens", 20),
        ("total_tokens", -1),
        ("total_tokens", True),
        ("total_tokens", 100.0),
        ("total_tokens", None),
        ("aggregate_ter", 1.1),
        ("aggregate_ter", -0.1),
        ("aggregate_ter", float("inf")),
        ("aggregate_ter", float("nan")),
        ("aggregate_ter", True),
        ("aligned_tokens", True),
        ("waste_tokens", False),
        ("aligned_tokens", -1),
        ("waste_tokens", 1.5),
    ],
)
def test_invalid_persisted_metrics_are_excluded_by_all_shared_readers(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    from tgv.bench.observed import observed_benchmark
    from tgv.linked_efficiency import linked_measurement_report

    repo = _repo(tmp_path)
    _evidence(repo, 40)
    metrics = {"aggregate_ter": 0.6, "aligned_tokens": 60, "waste_tokens": 40, "total_tokens": 100}
    metrics[field] = value
    database = repo / ".tgv" / "outcomes.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE ter_measurements SET metrics = ?", (json.dumps(metrics),))
        before = "\n".join(connection.iterdump())
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")
    assert proposal["invalid_measurement_count"] == 40
    assert report["coverage"]["invalid_measurement_count"] == 40
    assert proposal["efficiency_measurement_count"] == 0
    assert proposal["paired_task_count"] == 0
    assert proposal["evidence_availability"]["measurement_sample_minimum_met"] is False
    assert proposal["aggregate_ter"] is None
    assert proposal["waste_ratio"] is None
    assert proposal["application_permitted"] is False
    ledger = OutcomeLedger(database, read_only=True)
    assert observed_benchmark(ledger, repo)["coverage"]["invalid_measurement_count"] == 40
    assert linked_measurement_report(ledger, repo)["overall"]["invalid_measurements"] == 40
    with closing(sqlite3.connect(database)) as connection, connection:
        assert "\n".join(connection.iterdump()) == before


def test_valid_historical_measurements_without_total_remain_eligible(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40)
    with closing(sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3")) as connection, connection:
        connection.execute(
            "UPDATE ter_measurements SET metrics = json_remove(metrics, '$.total_tokens')"
        )
    proposal = _proposal(_report(repo, _config_path(tmp_path)), "local_code")
    assert proposal["efficiency_measurement_count"] == 40
    assert proposal["invalid_measurement_count"] == 0
    assert proposal["waste_ratio"] == 0.4


@pytest.mark.parametrize("absolute", [False, True])
def test_cli_uses_configured_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    absolute: bool,
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40)
    database = repo / ".tgv" / "outcomes.sqlite3"
    custom = repo / "custom.sqlite3"
    with (
        closing(sqlite3.connect(database)) as source,
        closing(sqlite3.connect(custom)) as destination,
    ):
        source.backup(destination)
    # Distinguish the old default ledger from the configured ledger.
    _evidence(repo, 1, success=False)
    path = _config_path(tmp_path)
    config = load_config(path)
    config.intelligence.outcome_database_path = custom if absolute else Path("custom.sqlite3")
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    before = custom.read_bytes()
    main(["calibrate", "--evidence", "--repo", str(repo), "--config", str(path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["total_task_count"] == 40
    assert _proposal(report, "local_code")["verified_failure_count"] == 0
    assert custom.read_bytes() == before


def test_missing_custom_ledger_is_not_created_or_replaced_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 40)
    path = _config_path(tmp_path)
    config = load_config(path)
    config.intelligence.outcome_database_path = Path("missing/outcomes.sqlite3")
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    original = path.read_bytes()
    main(
        ["calibrate", "--evidence", "--repo", str(repo), "--config", str(path), "--json", "--apply"]
    )
    assert json.loads(capsys.readouterr().out)["coverage"]["total_task_count"] == 0
    assert not (repo / "missing").exists()
    assert path.read_bytes() == original


@pytest.mark.parametrize("replacement_route", [Route.LOCAL_CODE, None])
def test_legacy_rerouting_clears_source_and_preserves_other_metadata(
    tmp_path: Path,
    replacement_route: Route | None,
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo, "first decision", route=Route.LOCAL_FAST)
    with closing(sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3")) as connection, connection:
        connection.execute("UPDATE outcomes SET metadata = json_set(metadata, '$.other', 42)")
    ledger = _ledger(repo)
    ledger.record_routing(
        task_id,
        revised_profile=TaskProfiler(default_config().intelligence).profile("Fix helper"),
        confidence=0.72,
        model="new-code",
        capability_tier=CapabilityTier.STANDARD,
        reasons=["legacy routing"],
        files=[],
        symbols=[],
        context_token_estimate=10,
        route=replacement_route,
    )
    record = ledger.query()[0]
    assert record.metadata == {
        "authoritative_route": (replacement_route or Route.LOCAL_FAST).value,
        "other": 42,
    }
    report = _report(repo, _config_path(tmp_path))
    assert report["coverage"]["eligible_task_count"] == 0
    assert report["coverage"]["routing_decision_source_counts"] == {"unavailable": 1}


@pytest.mark.parametrize("eligible", [False, True])
def test_apply_rejects_symlink_without_touching_target(tmp_path: Path, eligible: bool) -> None:
    repo = _repo(tmp_path)
    path = _config_path(tmp_path)
    report = _application_report(repo, path) if eligible else _report(repo, path)
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(path)
    original = path.read_bytes()
    with pytest.raises(EvidenceCalibrationError, match="symbolic link"):
        apply_evidence_calibration(report, alias, load_config(path), original)
    assert alias.is_symlink()
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.lock"))


@pytest.mark.parametrize("change", ["content", "identity", "symlink"])
def test_apply_rechecks_target_just_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    repo = _repo(tmp_path)
    path = _config_path(tmp_path)
    report = _application_report(repo, path)
    original = path.read_bytes()
    original_config = load_config(path)
    real_fsync = os.fsync
    replacement = tmp_path / "replacement.yaml"
    replacement.write_bytes(original)

    def concurrent_edit(descriptor: int) -> None:
        real_fsync(descriptor)
        if change == "content":
            path.write_bytes(original + b"\n# concurrent edit\n")
        elif change == "identity":
            os.replace(replacement, path)
        else:
            path.unlink()
            path.symlink_to(replacement)

    monkeypatch.setattr(calibration.os, "fsync", concurrent_edit)
    with pytest.raises(EvidenceCalibrationError, match=r"changed|symbolic link"):
        apply_evidence_calibration(report, path, original_config, original)
    assert not list(tmp_path.glob(".tgv.yaml.*.tmp"))
    assert load_config(path).routing.local_code_confidence == 0.68
    if change == "content":
        assert path.read_bytes().endswith(b"# concurrent edit\n")
    if change == "symlink":
        assert path.is_symlink()


def _application_process(
    report: dict[str, Any],
    path: Path,
    original: bytes,
    started: Any,
    entered: Any,
    release: Any,
    results: Any,
) -> None:
    original_config = default_config()
    snapshot = calibration._configuration_snapshot

    def pause_snapshot(target: Path) -> tuple[bytes, os.stat_result]:
        entered.set()
        if release is not None and not release.wait(10):
            raise RuntimeError("test release timeout")
        return snapshot(target)

    calibration._configuration_snapshot = pause_snapshot
    started.set()
    try:
        result = apply_evidence_calibration(report, path, original_config, original)
        results.put(result["applied"]["changed"])
    except EvidenceCalibrationError as exc:
        results.put(str(exc))


def test_cooperating_application_processes_serialize_and_reject_stale_snapshot(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    path = _config_path(tmp_path)
    path.chmod(0o640)
    report = _application_report(repo, path)
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    first_started, first_entered = context.Event(), context.Event()
    second_started, second_entered = context.Event(), context.Event()
    results = context.Queue()
    common = (report, path, path.read_bytes())
    first = context.Process(
        target=_application_process, args=(*common, first_started, first_entered, release, results)
    )
    second = context.Process(
        target=_application_process, args=(*common, second_started, second_entered, None, results)
    )
    try:
        first.start()
        assert first_entered.wait(10)
        second.start()
        assert second_started.wait(10)
        assert not second_entered.wait(0.2)
        release.set()
        first.join(10)
        second.join(10)
        assert first.exitcode == second.exitcode == 0
        outcomes = [results.get(timeout=2), results.get(timeout=2)]
        assert True in outcomes
        assert any(
            isinstance(value, str) and "Configuration changed" in value for value in outcomes
        )
        assert second_entered.is_set()
    finally:
        release.set()
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()
        results.join_thread()
    assert load_config(path).routing.local_code_confidence == 0.73
    assert path.stat().st_mode & 0o777 == 0o640
    assert (tmp_path / ".tgv.yaml.lock").is_file()
    assert not list(tmp_path.glob(".tgv.yaml.*.tmp"))


def test_no_change_apply_does_not_create_configuration_or_lock(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = default_config()
    report = evidence_calibration(_ledger(repo), config, repo)
    missing = tmp_path / "absent" / "tgv.yaml"
    result = apply_evidence_calibration(report, missing, config, b"")
    assert result["applied"] == {"requested": True, "changed": False, "thresholds": {}}
    assert not missing.parent.exists()


def test_verification_coverage_and_retry_weighting_are_explicit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _evidence(repo, 35)
    _evidence(repo, 5, success=None)
    report = _report(repo, _config_path(tmp_path))
    proposal = _proposal(report, "local_code")
    assert proposal["eligible_task_count"] == 40
    assert proposal["verification_coverage"] == 0.875
    assert proposal["paired_task_count"] == 35
    assert proposal["correctness_gate"]["passed"] is True
    assert proposal["application_permitted"] is False
    assert any("Repeated attempts" in limitation for limitation in report["limitations"])


@pytest.mark.parametrize("proposed", [0.68, 0.67])
def test_writer_rejects_noop_or_lowering_even_if_marked_eligible(
    tmp_path: Path,
    proposed: float,
) -> None:
    repo = _repo(tmp_path)
    path = _config_path(tmp_path)
    original = path.read_bytes()
    report = _application_report(repo, path)
    _proposal(report, "local_code")["proposed_threshold"] = proposed
    with pytest.raises(EvidenceCalibrationError, match="strictly raise"):
        apply_evidence_calibration(report, path, load_config(path), original)
    assert path.read_bytes() == original


def test_writer_fails_closed_without_posix_lock_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    repo = _repo(tmp_path)
    path = _config_path(tmp_path)
    report = _application_report(repo, path)
    config = load_config(path)
    original = path.read_bytes()
    original_import = builtins.__import__

    def no_fcntl(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fcntl":
            raise ImportError("not supported")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    with pytest.raises(EvidenceCalibrationError, match="requires POSIX"):
        apply_evidence_calibration(report, path, config, original)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".tgv.yaml.*"))


def test_writer_rejects_symlink_lock_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = _config_path(tmp_path)
    report = _application_report(repo, path)
    original = path.read_bytes()
    lock = tmp_path / ".tgv.yaml.lock"
    lock.symlink_to(path)
    with pytest.raises(EvidenceCalibrationError, match="atomically update"):
        apply_evidence_calibration(report, path, load_config(path), original)
    assert path.read_bytes() == original
    assert lock.is_symlink()


def test_cli_apply_requires_config_and_rejects_symlink(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["calibrate", "--evidence", "--repo", str(repo), "--apply"])
    assert exc_info.value.code == 2
    assert "--apply requires --config" in capsys.readouterr().err
    path = _config_path(tmp_path)
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(path)
    with pytest.raises(SystemExit) as exc_info:
        main(["calibrate", "--evidence", "--repo", str(repo), "--config", str(alias), "--apply"])
    assert exc_info.value.code == 2
    assert "symbolic link" in capsys.readouterr().err
    assert alias.is_symlink()


def test_apply_preserves_all_unrelated_configuration_values(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = _config_path(tmp_path)
    config = load_config(path)
    config.routing.maximum_tool_rounds = 8
    config.privacy.deny_patterns = ["**/private/**"]
    config.models["fast"].api_key = "test-only-secret"
    config.intelligence.outcome_database_path = Path("custom/outcomes.sqlite3")
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    path.chmod(0o600)
    report = _application_report(repo, path)
    result = apply_evidence_calibration(report, path, config, path.read_bytes())
    expected = config.model_dump(mode="json")
    expected["routing"]["local_code_confidence"] = 0.73
    assert load_config(path).model_dump(mode="json") == expected
    assert path.stat().st_mode & 0o777 == 0o600
    assert "test-only-secret" not in json.dumps(result)


def test_text_diagnostics_show_missing_efficiency_and_correctness_separately(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    report = _report(repo, _config_path(tmp_path))
    text = render_evidence_calibration(report)
    assert "TER=unavailable waste=unavailable" in text
    assert "correctness: passed=False" in text
    assert "intervention: application-permitted=False" in text
    assert "success=0 failure=0 unverified=0 incomplete=0" in text
