"""Final v0.39 release hardening: atomic completion, transitions, and measurements."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgv.cli import main
from tgv.config import default_config
from tgv.exceptions import (
    ConflictingMeasurementError,
    CorrelationMismatchError,
    CrossRepositoryError,
    LedgerWriteError,
)
from tgv.intelligence import TaskProfiler
from tgv.outcomes import OutcomeLedger, TerMeasurementRequest


def _setup(tmp_path: Path, request: str = "Fix the helper bug") -> tuple[Path, OutcomeLedger, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    ledger = OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")
    profile = TaskProfiler(default_config().intelligence).profile(request)
    task_id = ledger.create_pending(request, repo, profile)
    return repo, ledger, task_id


def _task(ledger: OutcomeLedger, task_id: str):
    return next(record for record in ledger.query(limit=1000) if record.task_id == task_id)


def _statuses(ledger: OutcomeLedger, task_id: str) -> dict[str, str]:
    return {attempt.attempt_id: attempt.status for attempt in ledger.execution_attempts(task_id)}


def _trigger(path: Path, sql: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(sql)


def test_complete_outcome_commits_all_writes_and_preserves_terminal_attempts(
    tmp_path: Path,
) -> None:
    repo, ledger, task_id = _setup(tmp_path)
    pending = ledger.create_execution_attempt(task_id)
    second_pending = ledger.create_execution_attempt(task_id)
    failed = ledger.create_execution_attempt(task_id, status="failed")

    completed = ledger.complete_outcome(
        task_id,
        accepted=True,
        expected_repository_id=ledger.repository_id(repo),
    )

    task = _task(ledger, task_id)
    assert completed == 2
    assert task.execution_status == "completed"
    assert task.human_accepted is True
    assert task.rework_required is False
    attempts = {attempt.attempt_id: attempt for attempt in ledger.execution_attempts(task_id)}
    assert attempts[pending.attempt_id].status == "completed"
    assert attempts[pending.attempt_id].completed_at is not None
    assert attempts[second_pending.attempt_id].status == "completed"
    assert (
        attempts[second_pending.attempt_id].completed_at
        == attempts[pending.attempt_id].completed_at
    )
    assert attempts[failed.attempt_id].status == "failed"
    assert attempts[failed.attempt_id].completed_at is None


def test_complete_outcome_preserves_execution_measurements(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    ledger.record_execution(
        task_id,
        status="running",
        latency_seconds=1.25,
        input_tokens=120,
        output_tokens=30,
        estimated_cost=0.004,
        failure_category=None,
        safety_violation=False,
    )

    ledger.complete_outcome(task_id, accepted=True)

    task = _task(ledger, task_id)
    assert task.execution_status == "completed"
    assert task.latency_seconds == 1.25
    assert task.input_tokens == 120
    assert task.output_tokens == 30
    assert task.estimated_cost == 0.004
    assert task.safety_violation is False
    assert _statuses(ledger, task_id) == {attempt.attempt_id: "completed"}


@pytest.mark.parametrize(
    "trigger",
    [
        "CREATE TRIGGER fail_execution BEFORE UPDATE OF execution_status ON outcomes "
        "WHEN NEW.execution_status = 'completed' "
        "BEGIN SELECT RAISE(ABORT, 'injected execution failure'); END",
        (
            "CREATE TRIGGER fail_feedback BEFORE UPDATE OF human_accepted, rework_required "
            "ON outcomes WHEN NEW.human_accepted IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT, 'injected feedback failure'); END"
        ),
        "CREATE TRIGGER fail_attempt BEFORE UPDATE OF status ON execution_attempts "
        "WHEN NEW.status = 'completed' "
        "BEGIN SELECT RAISE(ABORT, 'injected attempt failure'); END",
    ],
)
def test_complete_outcome_rolls_back_every_write_on_failure(tmp_path: Path, trigger: str) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    _trigger(ledger.path, trigger)

    with pytest.raises(LedgerWriteError, match="rolled back"):
        ledger.complete_outcome(task_id, accepted=True)

    task = _task(ledger, task_id)
    assert task.execution_status == "pending"
    assert task.human_accepted is None
    assert task.rework_required is None
    current = ledger.execution_attempts(task_id)[0]
    assert current.attempt_id == attempt.attempt_id
    assert current.status == "pending"
    assert current.completed_at is None


def test_complete_outcome_is_idempotent_and_preserves_completion_time(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)

    assert ledger.complete_outcome(task_id, accepted=True) == 1
    first = ledger.execution_attempts(task_id)[0]
    assert first.attempt_id == attempt.attempt_id
    assert first.completed_at is not None
    assert ledger.complete_outcome(task_id, accepted=True) == 0

    second = ledger.execution_attempts(task_id)[0]
    assert second.status == "completed"
    assert second.completed_at == first.completed_at
    task = _task(ledger, task_id)
    assert task.human_accepted is True
    assert task.rework_required is False


def test_complete_outcome_rejects_wrong_repository_without_writes(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)

    with pytest.raises(CrossRepositoryError):
        ledger.complete_outcome(task_id, accepted=True, expected_repository_id="other-repo")

    assert _task(ledger, task_id).execution_status == "pending"
    assert _statuses(ledger, task_id) == {attempt.attempt_id: "pending"}


def test_complete_outcome_rejects_correlated_attempt_mismatch(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE execution_attempts SET repository_id = 'other-repo' WHERE attempt_id = ?",
            (attempt.attempt_id,),
        )

    with pytest.raises(CorrelationMismatchError):
        ledger.complete_outcome(task_id, accepted=True)

    assert _task(ledger, task_id).execution_status == "pending"
    assert ledger.execution_attempts(task_id)[0].status == "pending"


def test_cli_completion_handles_missing_and_corrupt_ledgers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SystemExit) as missing:
        main(["outcomes", "complete", "missing-task", "--accepted", "--repo", str(repo)])
    assert missing.value.code == 2
    assert "Outcome ledger does not exist" in capsys.readouterr().err
    assert not (repo / ".tgv" / "outcomes.sqlite3").exists()

    database = repo / ".tgv" / "outcomes.sqlite3"
    database.parent.mkdir()
    database.write_bytes(b"not a sqlite database")
    with pytest.raises(SystemExit) as corrupt:
        main(["outcomes", "complete", "missing-task", "--accepted", "--repo", str(repo)])
    assert corrupt.value.code == 2
    error = capsys.readouterr().err
    assert "cannot be opened for completion" in error
    assert "Traceback" not in error


def test_cli_completion_handles_invalid_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, ledger, task_id = _setup(tmp_path)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("UPDATE schema_version SET version = 'corrupt'")

    with pytest.raises(SystemExit) as exc_info:
        main(["outcomes", "complete", task_id, "--accepted", "--repo", str(repo)])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "cannot be opened for completion" in error
    assert "Traceback" not in error


def test_completion_reports_unknown_tasks_with_an_existing_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, ledger, _task_id = _setup(tmp_path)
    with pytest.raises(KeyError, match="Unknown task id"):
        ledger.complete_outcome("missing-task", accepted=True)

    with pytest.raises(SystemExit) as exc_info:
        main(["outcomes", "complete", "missing-task", "--accepted", "--repo", str(repo)])
    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "Unknown task id: missing-task" in error
    assert "Traceback" not in error


def test_valid_attempt_transitions_and_idempotent_updates(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)

    for status in ("running", "running", "completed", "completed"):
        ledger.update_execution_attempt(attempt.attempt_id, status=status)
        assert ledger.execution_attempts(task_id)[0].status == status


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        ("completed", "running"),
        ("completed", "failed"),
        ("failed", "pending"),
        ("failed", "running"),
    ],
)
def test_terminal_attempts_cannot_return_to_active_states(
    tmp_path: Path, initial: str, target: str
) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id, status=initial)

    with pytest.raises(ValueError, match="Invalid execution attempt transition"):
        ledger.update_execution_attempt(attempt.attempt_id, status=target)

    assert ledger.execution_attempts(task_id)[0].status == initial


def test_running_attempt_cannot_return_to_pending(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id, status="running")

    with pytest.raises(ValueError, match=r"running.*pending"):
        ledger.update_execution_attempt(attempt.attempt_id, status="pending")

    assert ledger.execution_attempts(task_id)[0].status == "running"


def test_failed_attempt_retry_workflow_preserves_both_attempts(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    first = ledger.create_execution_attempt(task_id)
    ledger.update_execution_attempt(first.attempt_id, status="failed")
    second = ledger.create_execution_attempt(task_id)
    ledger.update_execution_attempt(second.attempt_id, status="running")
    ledger.update_execution_attempt(second.attempt_id, status="completed")

    assert _statuses(ledger, task_id) == {
        first.attempt_id: "failed",
        second.attempt_id: "completed",
    }


def test_unsupported_historical_attempt_status_is_not_rewritten(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE execution_attempts SET status = 'legacy-unknown' WHERE attempt_id = ?",
            (attempt.attempt_id,),
        )

    with pytest.raises(ValueError, match="unsupported historical status"):
        ledger.update_execution_attempt(attempt.attempt_id, status="completed")

    assert ledger.execution_attempts(task_id)[0].status == "legacy-unknown"


def test_attempt_creation_rejects_unknown_or_empty_status(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    with pytest.raises(ValueError, match="Unsupported execution attempt status"):
        ledger.create_execution_attempt(task_id, status="unknown")
    with pytest.raises(ValueError, match="must not be empty"):
        ledger.create_execution_attempt(task_id, status=" ")


def test_valid_measurement_persistence_and_generic_metadata(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    measurement = ledger.record_ter_measurement(
        attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version="3.0.0",
        metrics={"aggregate_ter": 0.8, "aligned_tokens": 80, "waste_tokens": 20},
    )
    generic = ledger.record_ter_measurement(
        attempt.attempt_id,
        analyzer="generic-analyzer",
        analyzer_version="1",
        metrics={"waste_ratio": 0.25, "tokens": 120, "notes": None},
    )

    assert measurement.metrics == {
        "aggregate_ter": 0.8,
        "aligned_tokens": 80,
        "waste_tokens": 20,
    }
    assert generic.metrics["waste_ratio"] == 0.25


@pytest.mark.parametrize(
    "metrics",
    [
        {"aggregate_ter": 1.0, "aligned_tokens": 0, "waste_tokens": 0},
        {"aggregate_ter": 0.5, "aligned_tokens": -1, "waste_tokens": 2},
        {"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": -2},
        {"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 2, "total_tokens": 4},
        {"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 2, "total_tokens": -3},
        {"aggregate_ter": 1.1, "aligned_tokens": 1, "waste_tokens": 1},
        {"aggregate_ter": -0.1, "aligned_tokens": 1, "waste_tokens": 1},
        {"aggregate_ter": float("nan"), "aligned_tokens": 1, "waste_tokens": 1},
        {"aggregate_ter": float("inf"), "aligned_tokens": 1, "waste_tokens": 1},
        {"aggregate_ter": True, "aligned_tokens": 1, "waste_tokens": 1},
        {"aggregate_ter": 10**400, "aligned_tokens": 1, "waste_tokens": 1},
        {"aggregate_ter": 0.5, "aligned_tokens": True, "waste_tokens": 1},
        {"aggregate_ter": 0.5, "aligned_tokens": 1.5, "waste_tokens": 1},
        {"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": False},
        {"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 1, "total_tokens": True},
        {"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 1, "total_tokens": 2.0},
        {"aggregate_ter": 0.5},
        {"total_tokens": 100},
    ],
)
def test_invalid_ter_measurements_are_rejected_before_persistence(
    tmp_path: Path, metrics: dict[str, float | int | None]
) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)

    with pytest.raises(ValueError):
        ledger.record_ter_measurement(
            attempt.attempt_id,
            analyzer="TER CLI",
            analyzer_version="3.0.0",
            metrics=metrics,
        )

    assert ledger.ter_measurements(attempt.attempt_id) == []


def test_batch_measurement_conflict_rolls_back_prior_insert(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    requests = [
        TerMeasurementRequest(
            attempt_id=attempt.attempt_id,
            analyzer="TER CLI",
            analyzer_version="3.0.0",
            metrics={"aggregate_ter": 0.8, "aligned_tokens": 80, "waste_tokens": 20},
        ),
        TerMeasurementRequest(
            attempt_id=attempt.attempt_id,
            analyzer="TER CLI",
            analyzer_version="3.0.0",
            metrics={"aggregate_ter": 0.5, "aligned_tokens": 50, "waste_tokens": 50},
        ),
    ]

    with pytest.raises(ConflictingMeasurementError):
        ledger.record_ter_measurements(requests)

    assert ledger.ter_measurements(attempt.attempt_id) == []


def test_historical_measurement_rows_remain_readable_without_rewrite(tmp_path: Path) -> None:
    _repo, ledger, task_id = _setup(tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    legacy_metrics = [
        {"aggregate_ter": 1.0, "aligned_tokens": 0, "waste_tokens": 0},
        {"aggregate_ter": 0.6, "aligned_tokens": 60, "waste_tokens": 40},
    ]
    with sqlite3.connect(ledger.path) as connection:
        for index, metrics in enumerate(legacy_metrics):
            connection.execute(
                "INSERT INTO ter_measurements "
                "(measurement_id, attempt_id, session_link_id, analyzer, analyzer_version, "
                "measured_at, metrics) VALUES (?, ?, NULL, ?, ?, ?, ?)",
                (
                    f"legacy-{index}",
                    attempt.attempt_id,
                    f"legacy-analyzer-{index}",
                    "1",
                    datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                    json.dumps(metrics),
                ),
            )

    measurements = ledger.ter_measurements(attempt.attempt_id)
    assert [measurement.metrics for measurement in measurements] == legacy_metrics
    with sqlite3.connect(ledger.path) as connection:
        stored = connection.execute(
            "SELECT metrics FROM ter_measurements WHERE attempt_id = ? ORDER BY measurement_id",
            (attempt.attempt_id,),
        ).fetchall()
    assert [json.loads(row[0]) for row in stored] == legacy_metrics
