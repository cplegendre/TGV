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
    ConflictingSessionLinkError,
    CorrelationMismatchError,
    CrossRepositoryError,
)
from tgv.intelligence import TaskProfiler
from tgv.models import CapabilityTier
from tgv.outcomes import OutcomeLedger
from tgv.repository import Repository
from tgv.service import TGVService

V1_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE outcomes (
    task_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    raw_request TEXT,
    task_category TEXT NOT NULL,
    initial_complexity_score REAL,
    revised_complexity_score REAL,
    routing_confidence REAL,
    selected_model TEXT,
    selected_capability_tier TEXT,
    routing_reasons TEXT NOT NULL DEFAULT '[]',
    retrieved_files TEXT NOT NULL DEFAULT '[]',
    retrieved_symbols TEXT NOT NULL DEFAULT '[]',
    context_token_estimate INTEGER,
    execution_status TEXT NOT NULL DEFAULT 'pending',
    tests_passed INTEGER,
    static_checks_passed INTEGER,
    human_accepted INTEGER,
    rework_required INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    latency_seconds REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    failure_category TEXT,
    safety_violation INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX outcomes_segment
    ON outcomes(task_category, selected_model, selected_capability_tier);
"""


def _seed_v1_database(path: Path, repository_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(V1_SCHEMA)
        connection.execute("INSERT INTO schema_version(version) VALUES (1)")
        connection.execute(
            "INSERT INTO outcomes (task_id, timestamp, repository_id, request_hash, "
            "task_category, selected_model, selected_capability_tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-routed",
                datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
                repository_id,
                "hash-routed",
                "bug_fix",
                "local-code",
                "standard",
            ),
        )
        connection.execute(
            "INSERT INTO outcomes (task_id, timestamp, repository_id, request_hash, "
            "task_category) VALUES (?, ?, ?, ?, ?)",
            (
                "legacy-pending",
                datetime(2024, 1, 2, tzinfo=UTC).isoformat(),
                repository_id,
                "hash-pending",
                "bug_fix",
            ),
        )


def _schema_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version FROM schema_version").fetchone()
    return int(row[0])


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {str(row[0]) for row in rows}


def _task(ledger: OutcomeLedger, root: Path, request: str = "fix helper bug") -> str:
    profile = TaskProfiler(default_config().intelligence).profile(request)
    task_id = ledger.create_pending(request, root, profile)
    ledger.record_routing(
        task_id,
        revised_profile=profile.model_copy(update={"stage": "revised"}),
        confidence=0.8,
        model="local-code",
        capability_tier=CapabilityTier.STANDARD,
        reasons=["bounded change"],
        files=["app.py"],
        symbols=["helper"],
        context_token_estimate=32,
    )
    return task_id


def test_new_database_creates_schema_v3(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.sqlite3"
    ledger = OutcomeLedger(path)
    assert ledger.query() == []
    assert _schema_version(path) == 3
    assert {
        "outcomes",
        "execution_attempts",
        "agent_sessions",
        "ter_measurements",
        "session_locators",
    } <= _table_names(path)


def test_migration_from_v1_preserves_records(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.sqlite3"
    repository_id = OutcomeLedger.repository_id(tmp_path)
    _seed_v1_database(path, repository_id)

    ledger = OutcomeLedger(path)
    records = ledger.query()

    assert {record.task_id for record in records} == {"legacy-routed", "legacy-pending"}
    assert _schema_version(path) == 3
    assert ledger.routing_decision_id_for("legacy-routed") is not None
    assert ledger.routing_decision_id_for("legacy-pending") is None
    assert "routing_decision_id" not in ledger.export_json(records)


def test_repeated_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.sqlite3"
    first = OutcomeLedger(path)
    task_id = _task(first, tmp_path)
    assert first.query()

    second = OutcomeLedger(path)
    assert second.query()[0].task_id == task_id
    third = OutcomeLedger(path)
    assert len(third.execution_attempts(task_id)) == 0
    assert _schema_version(path) == 3


def test_migration_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "outcomes.sqlite3"
    repository_id = OutcomeLedger.repository_id(tmp_path)
    _seed_v1_database(path, repository_id)

    def boom(connection: sqlite3.Connection) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(OutcomeLedger, "_create_v2_schema", staticmethod(boom))
    with pytest.raises(RuntimeError, match="migration failed"):
        OutcomeLedger(path).query()

    assert _schema_version(path) == 1
    assert {"execution_attempts", "agent_sessions", "ter_measurements"}.isdisjoint(
        _table_names(path)
    )
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outcomes)")}
    assert "routing_decision_id" not in columns


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version(version) VALUES (99)")

    with pytest.raises(RuntimeError, match="Unsupported outcome schema version: 99"):
        OutcomeLedger(path).query()


def test_routing_decision_id_is_stable_and_unknown_tasks_are_rejected(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "outcomes.sqlite3")
    task_id = _task(ledger, tmp_path)

    decision_id = ledger.routing_decision_id_for(task_id)
    assert decision_id is not None
    assert ledger.assign_routing_decision_id(task_id) == decision_id

    with pytest.raises(KeyError):
        ledger.routing_decision_id_for("missing")
    with pytest.raises(KeyError):
        ledger.assign_routing_decision_id("missing")


def test_create_execution_attempts_for_existing_task(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "outcomes.sqlite3")
    task_id = _task(ledger, tmp_path)
    decision_id = ledger.routing_decision_id_for(task_id)

    first = ledger.create_execution_attempt(task_id)
    second = ledger.create_execution_attempt(task_id, status="running")

    assert first.attempt_number == 1
    assert second.attempt_number == 2
    assert first.repository_id == OutcomeLedger.repository_id(tmp_path)
    assert first.routing_decision_id == decision_id
    assert [attempt.attempt_id for attempt in ledger.execution_attempts(task_id)] == [
        first.attempt_id,
        second.attempt_id,
    ]

    ledger.update_execution_attempt(
        first.attempt_id, status="completed", completed_at=datetime(2024, 3, 1, tzinfo=UTC)
    )
    updated = ledger.execution_attempts(task_id)[0]
    assert updated.status == "completed"
    assert updated.completed_at == datetime(2024, 3, 1, tzinfo=UTC)

    ledger.update_execution_attempt(second.attempt_id, status="failed")
    retained = ledger.execution_attempts(task_id)[1]
    assert retained.status == "failed"
    assert retained.completed_at is None


def test_execution_attempts_reject_invalid_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = OutcomeLedger(tmp_path / "outcomes.sqlite3")
    task_id = _task(ledger, tmp_path)

    with pytest.raises(KeyError):
        ledger.create_execution_attempt("missing")
    with pytest.raises(ValueError):
        ledger.create_execution_attempt(task_id, status="  ")
    with pytest.raises(CrossRepositoryError):
        ledger.create_execution_attempt(task_id, expected_repository_id="other-repository")
    with pytest.raises(KeyError):
        ledger.update_execution_attempt("missing", status="completed")
    with pytest.raises(ValueError):
        ledger.update_execution_attempt(
            ledger.create_execution_attempt(task_id).attempt_id, status=""
        )

    def locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.IntegrityError("attempt number conflict")

    monkeypatch.setattr(ledger, "_create_execution_attempt", locked)
    with pytest.raises(RuntimeError, match="Could not allocate an execution attempt"):
        ledger.create_execution_attempt(task_id)


def test_agent_session_links_are_namespaced_and_idempotent(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "outcomes.sqlite3")
    task_id = _task(ledger, tmp_path)
    first = ledger.create_execution_attempt(task_id)
    second = ledger.create_execution_attempt(task_id)
    content_hash = "a" * 64

    link = ledger.link_agent_session(
        first.attempt_id, agent="codex", external_session_id="session-1", content_hash=content_hash
    )
    repeat = ledger.link_agent_session(
        first.attempt_id, agent="codex", external_session_id="session-1", content_hash=content_hash
    )
    assert repeat.session_link_id == link.session_link_id
    assert ledger.agent_sessions(first.attempt_id) == [link]

    other_agent = ledger.link_agent_session(
        second.attempt_id, agent="claude", external_session_id="session-1"
    )
    assert other_agent.agent == "claude"
    assert other_agent.content_hash is None

    with pytest.raises(ConflictingSessionLinkError):
        ledger.link_agent_session(second.attempt_id, agent="codex", external_session_id="session-1")
    with pytest.raises(ConflictingSessionLinkError):
        ledger.link_agent_session(
            first.attempt_id, agent="codex", external_session_id="session-1", content_hash="b" * 64
        )
    with pytest.raises(KeyError):
        ledger.link_agent_session("missing", agent="codex", external_session_id="session-2")
    with pytest.raises(ValueError):
        ledger.link_agent_session(first.attempt_id, agent=" ", external_session_id="session-2")
    with pytest.raises(ValueError):
        ledger.link_agent_session(first.attempt_id, agent="codex", external_session_id="  ")
    with pytest.raises(ValueError):
        ledger.link_agent_session(
            first.attempt_id, agent="codex", external_session_id="session-2", content_hash="nope"
        )


def test_ter_measurements_link_to_attempts(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "outcomes.sqlite3")
    task_id = _task(ledger, tmp_path)
    first = ledger.create_execution_attempt(task_id)
    second = ledger.create_execution_attempt(task_id)
    link = ledger.link_agent_session(
        first.attempt_id, agent="codex", external_session_id="session-1"
    )
    other_link = ledger.link_agent_session(
        second.attempt_id, agent="codex", external_session_id="session-2"
    )

    measurement = ledger.record_ter_measurement(
        first.attempt_id,
        analyzer="ter",
        analyzer_version="3.0.0",
        metrics={"waste_ratio": 0.25, "tokens": 120, "notes": None},
        session_link_id=link.session_link_id,
    )
    assert measurement.metrics == {"notes": None, "tokens": 120, "waste_ratio": 0.25}
    assert ledger.ter_measurements(first.attempt_id) == [measurement]

    repeat = ledger.record_ter_measurement(
        first.attempt_id,
        analyzer="ter",
        analyzer_version="3.0.0",
        metrics={"waste_ratio": 0.25, "tokens": 120, "notes": None},
        session_link_id=link.session_link_id,
    )
    assert repeat.measurement_id == measurement.measurement_id

    with pytest.raises(ConflictingMeasurementError):
        ledger.record_ter_measurement(
            first.attempt_id,
            analyzer="ter",
            analyzer_version="3.0.0",
            metrics={"waste_ratio": 0.5},
            session_link_id=link.session_link_id,
        )
    with pytest.raises(CorrelationMismatchError):
        ledger.record_ter_measurement(
            first.attempt_id,
            analyzer="ter",
            analyzer_version="3.1.0",
            session_link_id=other_link.session_link_id,
        )
    with pytest.raises(KeyError):
        ledger.record_ter_measurement(
            first.attempt_id, analyzer="ter", analyzer_version="3.2.0", session_link_id="missing"
        )
    with pytest.raises(KeyError):
        ledger.record_ter_measurement("missing", analyzer="ter", analyzer_version="3.0.0")
    with pytest.raises(ValueError):
        ledger.record_ter_measurement(first.attempt_id, analyzer=" ", analyzer_version="3.0.0")
    with pytest.raises(ValueError):
        ledger.record_ter_measurement(first.attempt_id, analyzer="ter", analyzer_version=" ")
    with pytest.raises(ValueError):
        ledger.record_ter_measurement(
            first.attempt_id, analyzer="ter", analyzer_version="3.3.0", metrics={"ok": True}
        )
    with pytest.raises(ValueError):
        ledger.record_ter_measurement(
            first.attempt_id,
            analyzer="ter",
            analyzer_version="3.4.0",
            metrics={"bad": float("nan")},
        )
    with pytest.raises(ValueError):
        ledger.record_ter_measurement(
            first.attempt_id,
            analyzer="ter",
            analyzer_version="3.5.0",
            metrics={"nested": {"value": 1}},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError):
        ledger.record_ter_measurement(
            first.attempt_id, analyzer="ter", analyzer_version="3.6.0", metrics={" ": 1}
        )


def test_correlation_tables_store_no_content(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.sqlite3"
    ledger = OutcomeLedger(path)
    task_id = _task(ledger, tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    ledger.link_agent_session(attempt.attempt_id, agent="codex", external_session_id="session-1")
    ledger.record_ter_measurement(
        attempt.attempt_id, analyzer="ter", analyzer_version="3.0.0", metrics={"tokens": 1}
    )

    forbidden = ("prompt", "raw", "path", "jsonl", "transcript", "span")
    with sqlite3.connect(path) as connection:
        for table in ("execution_attempts", "agent_sessions", "ter_measurements"):
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
            for column in columns:
                assert not any(word in column for word in forbidden)
        raw_request = connection.execute(
            "SELECT raw_request FROM outcomes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert raw_request is None
    assert ledger.retain_raw_prompts is False


def test_existing_outcome_queries_and_exports_are_unchanged(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "outcomes.sqlite3")
    task_id = _task(ledger, tmp_path)
    attempt = ledger.create_execution_attempt(task_id)
    ledger.link_agent_session(attempt.attempt_id, agent="codex", external_session_id="session-9")

    records = ledger.query(model="local-code")
    assert [record.task_id for record in records] == [task_id]
    payload = json.loads(ledger.export_json(records))
    assert payload[0]["task_id"] == task_id
    assert "routing_decision_id" not in payload[0]
    assert "attempt_id" not in payload[0]
    assert ledger.export_csv(records).startswith("task_id,")


def test_cli_outcome_workflow_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = default_config()
    service = TGVService(
        config, Repository(tmp_path, config.repository, config.privacy.deny_patterns)
    )
    try:
        task_id = service.create_outcome("fix helper bug")
        profile = service.profile("fix helper bug", retrieve=False)
        service.outcomes.record_routing(
            task_id,
            revised_profile=profile,
            confidence=0.8,
            model="local-code",
            capability_tier=CapabilityTier.STANDARD,
            reasons=["test"],
            files=[],
            symbols=[],
            context_token_estimate=10,
        )
    finally:
        service.close()

    main(["outcomes", "list", "--repo", str(tmp_path), "--format", "json"])
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["task_id"] == task_id
    assert "routing_decision_id" not in listed[0]

    main(["outcomes", "complete", task_id, "--repo", str(tmp_path), "--accepted"])
    completed = json.loads(capsys.readouterr().out)
    assert completed == {"task_id": task_id, "completed": True, "accepted": True}
    assert OutcomeLedger(tmp_path / ".tgv" / "outcomes.sqlite3").query()[0].human_accepted is True
