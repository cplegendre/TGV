from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgv.cli import main
from tgv.config import default_config
from tgv.efficiency import TERAnalysis, TERAnalysisError, ter_analyzer
from tgv.exceptions import ConflictingMeasurementError, ConflictingSessionLinkError
from tgv.intelligence import TaskProfiler
from tgv.linked_efficiency import (
    LinkedEfficiencyError,
    linked_measurement_report,
    measure_linked_sessions,
)
from tgv.models import CapabilityTier, ExecutionAttempt, Route
from tgv.outcomes import OutcomeLedger
from tgv.session_linking import link_session_to_task

SESSION_ID = "8f3c4e5a-2b7d-4c1e-9a6f-1d2e3f4a5b6c"
OTHER_SESSION_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SECRET_TEXT = "SECRET-RAW-SESSION-CONTENT"


def _session(
    path: Path,
    *,
    session_id: str = SESSION_ID,
    text: str = "Fix the helper bug",
) -> Path:
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "message": {"role": "user", "content": text},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": "I fixed it."},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _ledger(repo: Path) -> OutcomeLedger:
    return OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")


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


def _link(repo: Path, task_id: str, session: Path):
    return link_session_to_task(
        _ledger(repo),
        task_id,
        repository_root=repo,
        session_path=session,
        agent="claude-code",
    )


def _analysis(path: Path, *, ter: float = 0.8, aligned: int = 80, waste: int = 20):
    session_id = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["sessionId"]
    return TERAnalysis(
        session_id=session_id,
        aggregate_ter=ter,
        aligned_tokens=aligned,
        waste_tokens=waste,
        total_tokens=aligned + waste,
        analyzer="fake-ter",
        analyzer_version="test",
    )


def _measure(repo: Path, analyzer=None):
    return measure_linked_sessions(
        _ledger(repo),
        repo,
        analyzer=analyzer or _analysis,
    )


def test_linked_session_measurement_persists_and_reports(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl", text=SECRET_TEXT)
    link_result = _link(repo, task_id, session)
    ledger = _ledger(repo)
    ledger.record_execution(task_id, status="completed")
    ledger.record_validation(task_id, tests_passed=True, static_checks_passed=True)

    results = _measure(repo)

    assert len(results) == 1
    measurement = ledger.ter_measurements(link_result.attempt.attempt_id)[0]
    assert measurement.session_link_id == link_result.link.session_link_id
    assert measurement.analyzer == "fake-ter"
    assert measurement.metrics == {
        "aggregate_ter": 0.8,
        "aligned_tokens": 80,
        "waste_tokens": 20,
        "total_tokens": 100,
    }
    report = linked_measurement_report(ledger, repo)
    assert report["overall"]["total_tasks"] == 1
    assert report["overall"]["linked_tasks"] == 1
    assert report["overall"]["successfully_measured_sessions"] == 1
    assert report["overall"]["verified_outcome_tasks"] == 1
    assert report["overall"]["verified_success_tasks"] == 1
    assert report["measurements"][0]["route"] == "local_code"
    assert report["measurements"][0]["selected_model"] == "local-code"
    assert report["measurements"][0]["selected_capability_tier"] == "standard"
    assert report["measurements"][0]["routing_decision_id"] is not None
    assert report["measurements"][0]["outcome_verification"] == "verified-success"


def test_repeated_linked_measurement_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)

    first = _measure(repo)[0].measurement
    second = _measure(repo)[0].measurement

    assert first.measurement_id == second.measurement_id
    assert len(_ledger(repo).ter_measurements(result.attempt.attempt_id)) == 1


def test_scoreboard_advanced_tier_uses_persisted_local_code_route(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(
        repo,
        tier=CapabilityTier.ADVANCED,
        model="advanced-model",
        route=Route.LOCAL_CODE,
    )
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)

    _measure(repo)
    report = linked_measurement_report(_ledger(repo), repo)

    assert report["measurements"][0]["selected_capability_tier"] == "advanced"
    assert report["measurements"][0]["route"] == "local_code"
    assert report["aggregates"]["by_route"]["local_code"]["measurement_count"] == 1


def test_deterministic_advanced_tier_uses_persisted_frontier_standard_route(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(
        repo,
        tier=CapabilityTier.ADVANCED,
        model="frontier-standard-model",
        route=Route.FRONTIER_STANDARD,
    )
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)

    _measure(repo)
    report = linked_measurement_report(_ledger(repo), repo)

    assert report["measurements"][0]["selected_capability_tier"] == "advanced"
    assert report["measurements"][0]["route"] == "frontier_standard"
    assert report["aggregates"]["by_route"]["frontier_standard"]["measurement_count"] == 1


def test_analysis_snapshot_isolates_validated_bytes_and_is_cleaned_up(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl", text=SECRET_TEXT)
    original_bytes = session.read_bytes()
    _link(repo, task_id, session)
    observed_snapshots: list[Path] = []

    def replace_original_after_validation(snapshot: Path) -> TERAnalysis:
        observed_snapshots.append(snapshot)
        _session(session, session_id=OTHER_SESSION_ID, text="replacement content")
        assert snapshot.read_bytes() == original_bytes
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o400
        return _analysis(snapshot)

    results = _measure(repo, replace_original_after_validation)

    assert results[0].link.external_session_id == SESSION_ID
    assert results[0].measurement.metrics["total_tokens"] == 100
    assert all(not path.exists() for path in observed_snapshots)


def test_analysis_snapshot_is_cleaned_up_on_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)
    observed_snapshots: list[Path] = []

    def fail(snapshot: Path) -> TERAnalysis:
        observed_snapshots.append(snapshot)
        raise TERAnalysisError("TER analysis failed")

    with pytest.raises(TERAnalysisError, match="TER analysis failed"):
        _measure(repo, fail)

    assert observed_snapshots
    assert all(not path.exists() for path in observed_snapshots)


def test_ter_session_identity_mismatch_is_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)

    def mismatched_identity(snapshot: Path) -> TERAnalysis:
        return replace(_analysis(snapshot), session_id=OTHER_SESSION_ID)

    with pytest.raises(LinkedEfficiencyError, match="TER session identity"):
        _measure(repo, mismatched_identity)

    assert _ledger(repo).ter_measurements(result.attempt.attempt_id) == []


def test_inconsistent_linked_analysis_metrics_are_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)

    def inconsistent_totals(snapshot: Path) -> TERAnalysis:
        return replace(_analysis(snapshot), total_tokens=1000)

    with pytest.raises(TERAnalysisError, match="total_tokens"):
        _measure(repo, inconsistent_totals)

    def out_of_range(snapshot: Path) -> TERAnalysis:
        return replace(_analysis(snapshot), aggregate_ter=1.1)

    with pytest.raises(TERAnalysisError, match="between 0 and 1"):
        _measure(repo, out_of_range)

    assert _ledger(repo).ter_measurements(result.attempt.attempt_id) == []


def test_missing_changed_and_mismatched_sessions_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)

    session.unlink()
    with pytest.raises(LinkedEfficiencyError, match="does not exist"):
        _measure(repo)

    _session(session, text="changed content")
    with pytest.raises(LinkedEfficiencyError, match="content changed"):
        _measure(repo)

    _session(session, session_id=OTHER_SESSION_ID)
    with pytest.raises(LinkedEfficiencyError, match="identity does not match"):
        _measure(repo)


def test_invalid_attempt_session_correlation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)
    ledger = _ledger(repo)
    mismatched = result.link.model_copy(update={"attempt_id": "other-attempt"})

    monkeypatch.setattr(
        ledger,
        "agent_sessions",
        lambda _attempt_id: [mismatched],
    )
    with pytest.raises(LinkedEfficiencyError, match="does not match its execution attempt"):
        linked_measurement_report(ledger, repo)


def test_ter_failure_and_malformed_json_are_actionable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)

    def fail(_path: Path):
        raise TERAnalysisError("TER analysis failed")

    with pytest.raises(TERAnalysisError, match="TER analysis failed"):
        _measure(repo, fail)

    executable = tmp_path / "fake-ter"
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then printf 'ter 3.0.0\\n'; exit 0; fi\n"
        "printf 'not-json'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    with pytest.raises(TERAnalysisError, match="invalid JSON"):
        measure_linked_sessions(
            _ledger(repo),
            repo,
            analyzer=ter_analyzer(executable=str(executable), timeout=5),
        )


def test_report_groups_route_model_tier_and_excludes_unverified_success(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_task = _task(repo, request="fix first bug", model="local-code")
    second_task = _task(
        repo,
        request="refactor architecture",
        tier=CapabilityTier.FRONTIER,
        model="frontier-model",
        route=Route.FRONTIER_DEEP,
    )
    first_session = _session(tmp_path / "first.jsonl")
    second_session = _session(
        tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID, text="Refactor architecture"
    )
    _link(repo, first_task, first_session)
    _link(repo, second_task, second_session)
    ledger = _ledger(repo)
    ledger.record_execution(first_task, status="completed")
    ledger.record_validation(first_task, tests_passed=True, static_checks_passed=True)
    ledger.record_execution(second_task, status="completed")

    results = _measure(repo)
    report = linked_measurement_report(ledger, repo)

    assert len(results) == 2
    assert report["overall"]["verified_success_tasks"] == 1
    assert report["overall"]["unverified_outcome_tasks"] == 1
    assert set(report["aggregates"]["by_route"]) == {"local_code", "frontier_deep"}
    assert set(report["aggregates"]["by_model"]) == {"local-code", "frontier-model"}
    assert set(report["aggregates"]["by_capability_tier"]) == {"standard", "frontier"}
    assert report["aggregates"]["by_route"]["local_code"]["verified_success_tasks"] == 1
    assert report["aggregates"]["by_route"]["frontier_deep"]["verified_success_tasks"] == 0


def test_multiple_execution_attempts_are_measured_separately(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    first = _session(tmp_path / "first.jsonl")
    second = _session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID)
    first_link = _link(repo, task_id, first)
    second_link = _link(repo, task_id, second)

    results = _measure(repo)

    assert first_link.attempt.attempt_number == 1
    assert second_link.attempt.attempt_number == 2
    assert {result.attempt.attempt_id for result in results} == {
        first_link.attempt.attempt_id,
        second_link.attempt.attempt_id,
    }
    ledger = _ledger(repo)
    ledger.record_ter_measurement(
        second_link.attempt.attempt_id,
        analyzer="other-analyzer",
        analyzer_version="1",
        metrics={"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 1},
        session_link_id=second_link.link.session_link_id,
    )
    report = linked_measurement_report(ledger, repo)
    assert report["overall"]["raw_measurement_count"] == 3
    assert report["overall"]["measurement_count"] == 2
    assert report["overall"]["superseded_measurements"] == 1
    assert report["overall"]["successfully_measured_sessions"] == 2


def test_repeated_analyzer_versions_do_not_double_count_a_session(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)
    _measure(repo)

    ledger = _ledger(repo)
    latest = ledger.record_ter_measurement(
        result.attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version="3.0.0",
        metrics={"aggregate_ter": 0.5, "aligned_tokens": 5, "waste_tokens": 5},
        session_link_id=result.link.session_link_id,
        measured_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    report = linked_measurement_report(ledger, repo)

    assert report["overall"]["raw_measurement_count"] == 2
    assert report["overall"]["measurement_count"] == 1
    assert report["overall"]["superseded_measurements"] == 1
    assert report["overall"]["total_tokens"] == 10
    latest_record = next(
        item
        for item in report["measurements"]
        if item["measurement_id"] == latest.measurement_id
    )
    assert latest_record["aggregate_selected"] is True


def test_later_ter_failure_rolls_back_the_measurement_batch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    first = _session(tmp_path / "first.jsonl")
    second = _session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID)
    first_link = _link(repo, task_id, first)
    second_link = _link(repo, task_id, second)
    calls = 0

    def fail_second(snapshot: Path) -> TERAnalysis:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TERAnalysisError("TER analysis failed")
        return _analysis(snapshot)

    with pytest.raises(TERAnalysisError, match="TER analysis failed"):
        _measure(repo, fail_second)

    ledger = _ledger(repo)
    assert ledger.ter_measurements(first_link.attempt.attempt_id) == []
    assert ledger.ter_measurements(second_link.attempt.attempt_id) == []


def test_later_measurement_conflict_rolls_back_the_measurement_batch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    first = _session(tmp_path / "first.jsonl")
    second = _session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID)
    first_link = _link(repo, task_id, first)
    second_link = _link(repo, task_id, second)
    ledger = _ledger(repo)
    existing = ledger.record_ter_measurement(
        second_link.attempt.attempt_id,
        analyzer="fake-ter",
        analyzer_version="test",
        metrics={"aggregate_ter": 0.1, "aligned_tokens": 1, "waste_tokens": 1},
        session_link_id=second_link.link.session_link_id,
    )

    with pytest.raises(ConflictingMeasurementError, match="different measurement"):
        _measure(repo)

    assert ledger.ter_measurements(first_link.attempt.attempt_id) == []
    assert ledger.ter_measurements(second_link.attempt.attempt_id) == [existing]


def test_v2_ledger_migration_preserves_links_and_reports_missing_locator(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)
    database_path = repo / ".tgv" / "outcomes.sqlite3"

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE session_locators")
        connection.execute("UPDATE schema_version SET version = 2")

    ledger = OutcomeLedger(database_path)
    ledger.query()
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert version == 3
    assert "session_locators" in tables
    assert ledger.agent_sessions(result.attempt.attempt_id)[0].session_path is None
    report = linked_measurement_report(ledger, repo)
    assert report["overall"]["linked_sessions_without_path"] == 1
    assert report["overall"]["missing_or_invalid_measurements"] == 1


def test_session_locator_conflicts_and_legacy_backfill(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    ledger = _ledger(repo)
    first_attempt = ledger.create_execution_attempt(task_id)
    second_attempt = ledger.create_execution_attempt(task_id)
    session = _session(tmp_path / "session.jsonl")
    path = str(session)
    content_hash = hashlib.sha256(session.read_bytes()).hexdigest()

    first = ledger.link_agent_session(
        first_attempt.attempt_id,
        agent="claude-code",
        external_session_id=SESSION_ID,
        content_hash=content_hash,
        session_path=path,
    )
    with pytest.raises(ConflictingSessionLinkError, match="already linked to another session"):
        ledger.link_agent_session(
            second_attempt.attempt_id,
            agent="claude-code",
            external_session_id=OTHER_SESSION_ID,
            content_hash=content_hash,
            session_path=path,
        )
    with pytest.raises(ConflictingSessionLinkError, match="different file path"):
        ledger.link_agent_session(
            first_attempt.attempt_id,
            agent="claude-code",
            external_session_id=SESSION_ID,
            content_hash=content_hash,
            session_path=str(tmp_path / "other.jsonl"),
        )

    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute("DELETE FROM session_locators")
    legacy = ledger.link_agent_session(
        first_attempt.attempt_id,
        agent="claude-code",
        external_session_id=SESSION_ID,
        content_hash=content_hash,
    )
    assert legacy.session_path is None
    assert legacy.session_link_id == first.session_link_id
    backfilled = ledger.link_agent_session(
        first_attempt.attempt_id,
        agent="claude-code",
        external_session_id=SESSION_ID,
        content_hash=content_hash,
        session_path=path,
    )
    assert backfilled.session_path == path

    result = _link(repo, task_id, session)
    assert result.link.session_path == str(session)
    other = _session(tmp_path / "other.jsonl")
    with pytest.raises(ConflictingSessionLinkError, match="different file path"):
        _link(repo, task_id, other)


def test_linked_report_rejects_invalid_associations_and_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)
    ledger = _ledger(repo)
    measurement = ledger.record_ter_measurement(
        result.attempt.attempt_id,
        analyzer="bad",
        analyzer_version="1",
        metrics={"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 1},
        session_link_id=result.link.session_link_id,
    )
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE ter_measurements SET metrics = ? WHERE measurement_id = ?",
            (json.dumps({"aggregate_ter": None}), measurement.measurement_id),
        )

    report = linked_measurement_report(ledger, repo)
    assert report["overall"]["invalid_measurements"] == 1

    wrong_attempt = measurement.model_copy(update={"attempt_id": "other-attempt"})
    monkeypatch.setattr(ledger, "ter_measurements", lambda _attempt_id: [wrong_attempt])
    with pytest.raises(LinkedEfficiencyError, match="invalid attempt association"):
        linked_measurement_report(ledger, repo)

    missing_link = measurement.model_copy(update={"session_link_id": "missing-link"})
    monkeypatch.setattr(ledger, "ter_measurements", lambda _attempt_id: [missing_link])
    with pytest.raises(LinkedEfficiencyError, match="invalid session association"):
        linked_measurement_report(ledger, repo)

    unlinked = measurement.model_copy(update={"session_link_id": None})
    monkeypatch.setattr(ledger, "ter_measurements", lambda _attempt_id: [unlinked])
    report = linked_measurement_report(ledger, repo)
    assert report["overall"]["invalid_measurements"] == 1

    wrong_task_attempt = ExecutionAttempt(
        attempt_id="other-attempt",
        task_id="other-task",
        repository_id=result.attempt.repository_id,
        attempt_number=2,
        started_at=result.attempt.started_at,
    )
    monkeypatch.setattr(
        ledger,
        "all_execution_attempts",
        lambda: [result.attempt, wrong_task_attempt],
    )
    with pytest.raises(LinkedEfficiencyError, match="has no outcome task"):
        linked_measurement_report(ledger, repo)


def test_linked_measurement_rejects_repository_and_link_metadata_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    result = _link(repo, task_id, session)
    ledger = _ledger(repo)

    with pytest.raises(LinkedEfficiencyError, match="belongs to a different repository"):
        linked_measurement_report(ledger, tmp_path / "other-repo")

    wrong_repository = result.attempt.model_copy(update={"repository_id": "other-repository"})
    monkeypatch.setattr(ledger, "all_execution_attempts", lambda: [wrong_repository])
    with pytest.raises(LinkedEfficiencyError, match="does not match its outcome task"):
        linked_measurement_report(ledger, repo)
    monkeypatch.setattr(ledger, "all_execution_attempts", lambda: [result.attempt])

    missing_path = result.link.model_copy(update={"session_path": None})
    monkeypatch.setattr(ledger, "agent_sessions", lambda _attempt_id: [missing_path])
    with pytest.raises(LinkedEfficiencyError, match="no explicit session path"):
        measure_linked_sessions(ledger, repo, analyzer=_analysis)

    missing_hash = result.link.model_copy(update={"content_hash": None})
    monkeypatch.setattr(ledger, "agent_sessions", lambda _attempt_id: [missing_hash])
    with pytest.raises(LinkedEfficiencyError, match="no stored content fingerprint"):
        measure_linked_sessions(ledger, repo, analyzer=_analysis)


def test_unassigned_routing_metadata_is_reported_explicitly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ledger = _ledger(repo)
    profile = TaskProfiler(default_config().intelligence).profile("fix helper bug")
    task_id = ledger.create_pending("fix helper bug", repo, profile)
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)
    _measure(repo)
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE outcomes SET selected_model = NULL, selected_capability_tier = NULL "
            "WHERE task_id = ?",
            (task_id,),
        )

    report = linked_measurement_report(ledger, repo)
    assert report["measurements"][0]["route"] == "unavailable"
    assert report["measurements"][0]["selected_model"] == "unassigned"
    assert report["aggregates"]["by_route"]["unavailable"]["measurement_count"] == 1


def test_verified_failure_and_cross_task_measurement_are_rejected_or_labeled(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_task = _task(repo, request="fix first bug")
    second_task = _task(repo, request="fix second bug")
    first_session = _session(tmp_path / "first.jsonl")
    second_session = _session(
        tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID, text="Fix second bug"
    )
    first = _link(repo, first_task, first_session)
    second = _link(repo, second_task, second_session)
    ledger = _ledger(repo)
    measurement = ledger.record_ter_measurement(
        first.attempt.attempt_id,
        analyzer="fake-ter",
        analyzer_version="test",
        metrics={"aggregate_ter": 0.1, "aligned_tokens": 1, "waste_tokens": 1},
        session_link_id=first.link.session_link_id,
    )
    ledger.record_execution(first_task, status="completed")
    ledger.record_validation(first_task, tests_passed=False, static_checks_passed=None)

    report = linked_measurement_report(ledger, repo)
    assert report["measurements"][0]["outcome_verification"] == "verified-failure"

    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE ter_measurements SET session_link_id = ? WHERE measurement_id = ?",
            (second.link.session_link_id, measurement.measurement_id),
        )
    with pytest.raises(LinkedEfficiencyError, match="invalid outcome association"):
        linked_measurement_report(ledger, repo)


def test_linked_cli_text_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)
    monkeypatch.setattr("tgv.linked_efficiency.ter_analyzer", lambda **_kwargs: _analysis)

    main(["efficiency", "--linked", "--repo", str(repo)])
    output = capsys.readouterr().out
    assert "source: linked-ter" in output
    assert "by_route:local_code" in output


def test_empty_ledger_and_deterministic_linked_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def unexpected(*args, **kwargs):
        raise AssertionError("linked mode must not invoke TER without a linked session")

    monkeypatch.setattr("tgv.linked_efficiency.ter_analyzer", unexpected)
    main(["efficiency", "--linked", "--repo", str(repo), "--format", "json"])
    first = capsys.readouterr().out
    report = json.loads(first)
    assert report["overall"]["total_tasks"] == 0
    assert report["measurements"] == []

    main(["report", "--linked", "--repo", str(repo), "--json"])
    second = capsys.readouterr().out
    assert first == second


def test_linked_cli_uses_injected_public_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl")
    _link(repo, task_id, session)
    monkeypatch.setattr("tgv.linked_efficiency.ter_analyzer", lambda **_kwargs: _analysis)
    main(["efficiency", "--linked", "--repo", str(repo), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "linked-ter"
    assert report["overall"]["measurement_count"] == 1


def test_linked_efficiency_rejects_explicit_sessions_option(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "efficiency",
                "--linked",
                "--sessions",
                str(sessions),
                "--repo",
                str(repo),
            ]
        )

    assert exc_info.value.code == 2
    assert "cannot be combined with --sessions" in capsys.readouterr().err


def test_legacy_efficiency_and_report_commands_remain_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session.jsonl").write_text(
        '{"route_decision": {"route": "local_fast"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        "tgv.cli.score_sessions",
        lambda _path: [],
    )
    main(["efficiency", "--format", "json"])
    assert json.loads(capsys.readouterr().out) == {}

    main(["report", "--sessions", str(sessions), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["sessions"] == 1
    assert report["local_share"] == 1.0


def test_no_raw_session_content_is_persisted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl", text=SECRET_TEXT)
    _link(repo, task_id, session)
    _measure(repo)

    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        dump = "\n".join(connection.iterdump())
    assert SECRET_TEXT not in dump
    assert "I fixed it." not in dump
