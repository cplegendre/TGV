from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgv.bench.observed import observed_benchmark
from tgv.calibration import evidence_calibration
from tgv.cli import main
from tgv.confidence import confidence_report
from tgv.config import default_config
from tgv.exceptions import LedgerReadError
from tgv.intelligence import TaskProfiler
from tgv.linked_efficiency import linked_measurement_report
from tgv.models import CapabilityTier, Route
from tgv.outcomes import (
    OutcomeLedger,
    _coerce_finite_number,
    _row_value,
    safe_json_object,
)
from tgv.session_linking import link_session_to_task

SESSION_ID = "8f3c4e5a-2b7d-4c1e-9a6f-1d2e3f4a5b6c"
OTHER_SESSION_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SECRET = "SECRET-RAW-METADATA-VALUE"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _ledger(repo: Path) -> OutcomeLedger:
    return OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")


def _session(path: Path, *, session_id: str = SESSION_ID) -> Path:
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "message": {"role": "user", "content": "Fix the helper bug"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": "I fixed it."},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _task(
    repo: Path,
    *,
    request: str = "fix helper bug",
    tier: CapabilityTier = CapabilityTier.STANDARD,
    model: str = "local-code",
    route: Route = Route.LOCAL_CODE,
) -> str:
    ledger = _ledger(repo)
    profile = TaskProfiler(default_config().intelligence).profile(request)
    task_id = ledger.create_pending(request, repo, profile)
    ledger.record_routing(
        task_id,
        revised_profile=profile,
        confidence=0.8,
        model=model,
        capability_tier=tier,
        reasons=["test"],
        files=[],
        symbols=[],
        context_token_estimate=10,
        route=route,
    )
    return task_id


def _verified_task(
    repo: Path,
    tmp_path: Path,
    *,
    session_id: str = SESSION_ID,
    request: str = "fix helper bug",
    success: bool = True,
    measured: bool = True,
) -> str:
    task_id = _task(repo, request=request)
    ledger = _ledger(repo)
    link = link_session_to_task(
        ledger,
        task_id,
        repository_root=repo,
        session_path=_session(tmp_path / f"{session_id}.jsonl", session_id=session_id),
        agent="claude-code",
    )
    if measured:
        ledger.record_ter_measurement(
            link.attempt.attempt_id,
            analyzer="TER CLI",
            analyzer_version="3.0.0",
            metrics={
                "aggregate_ter": 0.8,
                "aligned_tokens": 80,
                "waste_tokens": 20,
                "total_tokens": 100,
            },
            session_link_id=link.link.session_link_id,
            measured_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    ledger.record_execution(task_id, status="completed")
    ledger.record_validation(task_id, tests_passed=success, static_checks_passed=None)
    return task_id


def _update(repo: Path, statement: str, params: tuple[object, ...]) -> None:
    database = _ledger(repo).path
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(statement, params)


SQLITE_COORDINATION_SUFFIXES = ("-shm", "-wal", "-journal")


def _snapshot(repo: Path) -> dict[str, bytes]:
    """Snapshot repository files, excluding SQLite WAL coordination artifacts."""
    return {
        str(path.relative_to(repo)): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and not path.name.endswith(SQLITE_COORDINATION_SUFFIXES)
    }


def _invalid_records(payload: dict) -> dict:
    for container in (payload, payload.get("overall", {}), payload.get("coverage", {})):
        if "invalid_records" in container:
            return container["invalid_records"]
    return {}


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = 0
    try:
        main(argv)
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


REPORT_COMMANDS = (
    ("report", "--linked"),
    ("benchmark", "observed"),
    ("calibrate", "--evidence"),
    ("confidence",),
)


def _cli_argv(command: tuple[str, ...], repo: Path) -> list[str]:
    return [*command, "--repo", str(repo), "--json"]


@pytest.mark.parametrize("command", REPORT_COMMANDS)
def test_reporting_commands_do_not_create_a_missing_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: tuple[str, ...]
) -> None:
    repo = _repo(tmp_path)
    code, out, err = _run(capsys, _cli_argv(command, repo))
    assert code == 0, err
    assert not (repo / ".tgv").exists()
    payload = json.loads(out)
    assert _invalid_records(payload).get("excluded_tasks", 0) == 0


def test_report_linked_missing_ledger_is_empty_and_documented(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    code, out, err = _run(capsys, ["report", "--linked", "--repo", str(repo), "--json"])
    assert code == 0, err
    report = json.loads(out)
    assert report["status"] == "empty"
    assert report["overall"]["total_tasks"] == 0
    assert report["overall"]["linked_tasks"] == 0
    assert report["measurements"] == []
    assert report["overall"]["invalid_records"]["excluded_tasks"] == 0
    assert not (repo / ".tgv").exists()


def test_linked_report_text_renders_zero_token_aggregate_as_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    task_id = _verified_task(repo, tmp_path, measured=False)
    ledger = _ledger(repo)
    attempt = ledger.execution_attempts(task_id)[0]
    link = ledger.agent_sessions(attempt.attempt_id)[0]
    _update(
        repo,
        "INSERT INTO ter_measurements "
        "(measurement_id, attempt_id, session_link_id, analyzer, analyzer_version, "
        "measured_at, metrics) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-zero-token",
            attempt.attempt_id,
            link.session_link_id,
            "TER CLI",
            "3.0.0",
            datetime.now(UTC).isoformat(),
            json.dumps({"aggregate_ter": 1.0, "aligned_tokens": 0, "waste_tokens": 0}),
        ),
    )
    code, out, err = _run(capsys, ["report", "--linked", "--repo", str(repo)])
    assert code == 0, err
    assert "n/a" in out
    assert "0.0000" not in out


def test_outcomes_read_commands_do_not_create_or_mutate_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    for action in ("list", "export"):
        code, _, err = _run(capsys, ["outcomes", action, "--repo", str(repo), "--format", "json"])
        assert code == 0, err
        assert not (repo / ".tgv").exists()

    _verified_task(repo, tmp_path)
    before = _snapshot(repo)
    for action in ("list", "export"):
        code, out, err = _run(capsys, ["outcomes", action, "--repo", str(repo), "--format", "json"])
        assert code == 0, err
        assert json.loads(out)
    assert _snapshot(repo) == before


def test_empty_existing_ledger_reports_zeros_without_migration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")
    before = _snapshot(repo)
    for command in REPORT_COMMANDS:
        code, out, err = _run(capsys, _cli_argv(command, repo))
        assert code == 0, err
        assert _invalid_records(json.loads(out)).get("excluded_tasks", 0) == 0
    assert _snapshot(repo) == before
    report = linked_measurement_report(OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3"), repo)
    assert report["status"] == "empty"
    assert report["overall"]["total_tasks"] == 0
    assert (
        observed_benchmark(OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3"), repo)["status"]
        == "empty"
    )


def test_valid_fixture_statistics_are_unchanged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path, request="first task")
    _verified_task(repo, tmp_path, session_id=OTHER_SESSION_ID, request="second task")
    ledger = _ledger(repo)

    linked = linked_measurement_report(ledger, repo)
    assert linked["overall"]["aggregate_ter"] == 0.8
    assert linked["overall"]["measurement_count"] == 2
    assert linked["overall"]["total_tokens"] == 200
    assert linked["overall"]["verified_success_tasks"] == 2
    assert linked["overall"]["invalid_records"]["invalid_metadata_tasks"] == 0

    observed = observed_benchmark(ledger, repo)
    assert observed["status"] == "observational"
    assert observed["coverage"]["verified_success_count"] == 2
    assert observed["coverage"]["verified_success_rate"] == 1.0
    assert observed["coverage"]["invalid_measurement_count"] == 0

    confidence = confidence_report(ledger, repo)
    assert confidence["overall"]["scored_observations"] == 2
    assert confidence["overall"]["brier_score"] == 0.04
    assert confidence["overall"]["expected_calibration_error"] == 0.2


def test_reporting_commands_do_not_mutate_an_existing_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    before = _snapshot(repo)
    for command in REPORT_COMMANDS:
        code, _, err = _run(capsys, _cli_argv(command, repo))
        assert code == 0, err
    assert _snapshot(repo) == before


def test_malformed_metadata_is_counted_and_valid_rows_survive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path, request="first task", success=True)
    broken = _verified_task(
        repo, tmp_path, session_id=OTHER_SESSION_ID, request="second task", success=False
    )
    _update(repo, "UPDATE outcomes SET metadata = ? WHERE task_id = ?", ("[]", broken))
    ledger = _ledger(repo)

    linked = linked_measurement_report(ledger, repo)
    assert linked["status"] == "observational"
    assert linked["overall"]["total_tasks"] == 2
    assert linked["overall"]["verified_success_tasks"] == 1
    assert linked["overall"]["verified_failure_tasks"] == 1
    assert linked["overall"]["invalid_records"]["invalid_metadata_tasks"] == 1
    assert linked["overall"]["invalid_records"]["excluded_tasks"] == 0

    observed = observed_benchmark(ledger, repo)
    assert observed["coverage"]["verified_outcome_count"] == 2
    assert observed["coverage"]["invalid_records"]["invalid_metadata_tasks"] == 1

    calibration = evidence_calibration(ledger, default_config(), repo)
    assert calibration["coverage"]["invalid_records"]["invalid_metadata_tasks"] == 1

    confidence = confidence_report(ledger, repo)
    assert confidence["overall"]["scored_observations"] == 2
    assert confidence["overall"]["invalid_confidence_tasks"] == 0


def test_unparsable_metadata_never_leaks_into_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    task_id = _verified_task(repo, tmp_path)
    _update(repo, "UPDATE outcomes SET metadata = ? WHERE task_id = ?", (SECRET, task_id))
    for command in REPORT_COMMANDS:
        code, out, err = _run(capsys, _cli_argv(command, repo))
        assert code == 0, err
        assert SECRET not in out + err


def test_unreadable_task_rows_are_quarantined_with_dependents(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path, request="valid task")
    broken = _verified_task(
        repo, tmp_path, session_id=OTHER_SESSION_ID, request="broken task", success=False
    )
    _update(
        repo,
        "UPDATE outcomes SET task_category = ? WHERE task_id = ?",
        ("not-a-category", broken),
    )
    ledger = _ledger(repo)

    linked = linked_measurement_report(ledger, repo)
    assert linked["overall"]["total_tasks"] == 1
    assert linked["overall"]["verified_outcome_tasks"] == 1
    assert linked["overall"]["verified_success_tasks"] == 1
    assert linked["overall"]["verified_failure_tasks"] == 0
    assert linked["overall"]["invalid_records"]["excluded_tasks"] == 1
    assert linked["overall"]["invalid_records"]["excluded_attempts"] == 1
    assert linked["overall"]["invalid_records"]["excluded_sessions"] == 0
    assert linked["overall"]["invalid_records"]["excluded_measurements"] == 0

    observed = observed_benchmark(ledger, repo)
    assert observed["coverage"]["invalid_records"]["excluded_tasks"] == 1
    assert observed["coverage"]["verified_outcome_count"] == 1

    calibration = evidence_calibration(ledger, default_config(), repo)
    assert calibration["coverage"]["invalid_records"]["excluded_tasks"] == 1
    assert calibration["coverage"]["total_task_count"] == 1

    # Outside evidence reporting, a strict read fails closed instead of skipping rows.
    with pytest.raises(LedgerReadError, match="unreadable task record"):
        _ledger(repo).query(limit=10)


def test_invalid_measurement_metrics_are_counted_not_fatal(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    _update(repo, "UPDATE ter_measurements SET metrics = ?", ("not-json",))
    ledger = _ledger(repo)

    linked = linked_measurement_report(ledger, repo)
    assert linked["overall"]["invalid_records"]["invalid_measurement_metrics"] == 1
    assert linked["overall"]["invalid_measurements"] == 1
    assert linked["overall"]["successfully_measured_sessions"] == 0

    observed = observed_benchmark(ledger, repo)
    assert observed["coverage"]["invalid_measurement_count"] == 1
    assert observed["coverage"]["invalid_records"]["invalid_measurement_metrics"] == 1


def test_invalid_confidence_is_counted_and_not_scored(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _verified_task(repo, tmp_path)
    _update(repo, "UPDATE outcomes SET routing_confidence = ? WHERE task_id = ?", ("abc", task_id))
    ledger = _ledger(repo)

    linked = linked_measurement_report(ledger, repo)
    assert linked["overall"]["total_tasks"] == 1
    assert linked["overall"]["invalid_records"]["invalid_confidence_tasks"] == 1
    assert linked["measurements"][0]["routing_confidence"] is None

    observed = observed_benchmark(ledger, repo)
    assert observed["coverage"]["verified_outcome_count"] == 1
    assert observed["coverage"]["invalid_records"]["invalid_confidence_tasks"] == 1

    confidence = confidence_report(ledger, repo)
    assert confidence["overall"]["scored_observations"] == 0
    assert confidence["overall"]["invalid_confidence_tasks"] == 1
    assert confidence["overall"]["verified_success_rate"] == 1.0


def test_unreadable_attempts_are_quarantined(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    _update(repo, "UPDATE execution_attempts SET started_at = ?", ("not-a-date",))
    linked = linked_measurement_report(_ledger(repo), repo)
    assert linked["overall"]["total_tasks"] == 1
    assert linked["overall"]["linked_tasks"] == 0
    assert linked["overall"]["invalid_records"]["excluded_attempts"] == 1
    assert linked["overall"]["invalid_records"]["excluded_sessions"] == 0


def test_unreadable_session_links_quarantine_dependent_measurements(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    _update(repo, "UPDATE agent_sessions SET linked_at = ?", ("not-a-date",))
    linked = linked_measurement_report(_ledger(repo), repo)
    assert linked["overall"]["linked_tasks"] == 0
    assert linked["overall"]["invalid_records"]["excluded_sessions"] == 1
    assert linked["overall"]["invalid_records"]["excluded_measurements"] == 1
    assert linked["overall"]["successfully_measured_sessions"] == 0


def test_unreadable_measurement_rows_are_counted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    _update(repo, "UPDATE ter_measurements SET measured_at = ?", ("not-a-date",))
    linked = linked_measurement_report(_ledger(repo), repo)
    assert linked["overall"]["invalid_records"]["excluded_measurements"] == 1
    assert linked["overall"]["successfully_measured_sessions"] == 0
    assert linked["overall"]["missing_or_invalid_measurements"] == 1


def test_strict_reads_fail_closed_on_unreadable_measurements(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    attempt_id = _ledger(repo).all_execution_attempts()[0].attempt_id
    _update(repo, "UPDATE ter_measurements SET metrics = ?", ("not-json",))
    with pytest.raises(LedgerReadError, match="unreadable measurement record"):
        _ledger(repo).ter_measurements(attempt_id)


def test_quarantine_without_identifiers_still_counts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    ledger = _ledger(repo)
    with ledger.evidence_read() as diagnostics:
        ledger._quarantine({}, "task")
        ledger._quarantine({}, "session")
    assert diagnostics.excluded_tasks == 1
    assert diagnostics.excluded_sessions == 1
    assert diagnostics.quarantined_task_ids == set()
    assert diagnostics.quarantined_session_link_ids == set()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (True, None),
        (0.5, 0.5),
        ("0.5", 0.5),
        ("nan", None),
        ("inf", None),
        ("abc", None),
    ],
)
def test_coerce_finite_number(raw: object, expected: float | None) -> None:
    assert _coerce_finite_number(raw) == expected


def test_row_value_missing_column_returns_none() -> None:
    assert _row_value({}, "task_id") is None


@pytest.mark.parametrize("raw", [None, 7, "[]", "not-json", '{"k": 1}'])
def test_safe_json_object_never_raises(raw: object) -> None:
    if raw == '{"k": 1}':
        assert safe_json_object(raw) == {"k": 1}
    else:
        assert safe_json_object(raw) in (None,)


@pytest.mark.parametrize("command", REPORT_COMMANDS)
def test_unsupported_schema_fails_safely_without_migration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: tuple[str, ...]
) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    _update(repo, "UPDATE schema_version SET version = ?", (99,))
    before = _snapshot(repo)

    code, out, err = _run(capsys, _cli_argv(command, repo))
    assert code == 2
    assert "not supported in read-only mode" in err
    assert "Traceback" not in err
    assert SECRET not in out + err
    assert _snapshot(repo) == before


@pytest.mark.parametrize("command", REPORT_COMMANDS)
def test_reporting_json_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: tuple[str, ...]
) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path, request="first task")
    _verified_task(repo, tmp_path, session_id=OTHER_SESSION_ID, request="second task")
    first = _run(capsys, _cli_argv(command, repo))
    second = _run(capsys, _cli_argv(command, repo))
    assert first[0] == second[0] == 0
    assert first[1] == second[1]


def test_outcomes_list_reports_corrupt_rows_without_raw_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    task_id = _verified_task(repo, tmp_path)
    _update(repo, "UPDATE outcomes SET metadata = ? WHERE task_id = ?", (SECRET, task_id))
    code, out, err = _run(capsys, ["outcomes", "list", "--repo", str(repo), "--format", "json"])
    assert code == 2
    assert "unreadable task record" in err
    assert SECRET not in out + err


def test_evidence_read_context_is_nested_and_restores_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verified_task(repo, tmp_path)
    ledger = _ledger(repo)
    assert ledger.read_only is False
    with ledger.evidence_read() as outer:
        with ledger.evidence_read() as inner:
            assert inner is outer
        assert ledger.diagnostics is outer
    assert ledger.diagnostics is None
    task_id = _ledger(repo).query(limit=1)[0].task_id
    with pytest.raises(sqlite3.OperationalError, match="readonly"), ledger.evidence_read():
        ledger.record_execution(task_id, status="completed")
