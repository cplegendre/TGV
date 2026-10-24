"""Phase 3 contract regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgv.cli import main
from tgv.confidence import ConfidenceObservation, _bootstrap_intervals, confidence_report
from tgv.config import default_config
from tgv.exceptions import ConflictingSessionLinkError, CrossRepositoryError
from tgv.intelligence import TaskProfiler
from tgv.linked_efficiency import LinkedEfficiencyError, measure_linked_sessions
from tgv.outcomes import OutcomeLedger
from tgv.session_linking import link_session_to_task


def test_replace_unmeasured_preserves_link_and_rejects_measured(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "db.sqlite3")
    profile = TaskProfiler(default_config().intelligence).profile("Fix test")
    task = ledger.create_pending("Fix test", tmp_path, profile)
    repo_id = ledger.repository_id(tmp_path)
    attempt, linked, _ = ledger.link_agent_session_for_task(
        task,
        agent="claude-code",
        external_session_id="session-1",
        content_hash="a" * 64,
        session_path=str(tmp_path / "old.jsonl"),
        expected_repository_id=repo_id,
    )
    _, newer = ledger.replace_unmeasured_session(
        task,
        attempt_id=attempt.attempt_id,
        agent="claude-code",
        external_session_id="session-1",
        content_hash="b" * 64,
        session_path=str(tmp_path / "new.jsonl"),
        expected_repository_id=repo_id,
    )
    assert newer.session_link_id == linked.session_link_id
    assert newer.content_hash == "b" * 64
    with pytest.raises(ConflictingSessionLinkError, match="identity"):
        ledger.replace_unmeasured_session(
            task,
            attempt_id=attempt.attempt_id,
            agent="claude-code",
            external_session_id="different",
            content_hash="c" * 64,
            session_path=str(tmp_path / "new.jsonl"),
            expected_repository_id=repo_id,
        )
    ledger.record_ter_measurement(
        attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version="3.0.0",
        metrics={"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 1},
        session_link_id=linked.session_link_id,
    )
    with pytest.raises(ConflictingSessionLinkError, match="Measured"):
        ledger.replace_unmeasured_session(
            task,
            attempt_id=attempt.attempt_id,
            agent="claude-code",
            external_session_id="session-1",
            content_hash="c" * 64,
            session_path=str(tmp_path / "new.jsonl"),
            expected_repository_id=repo_id,
        )


def test_legacy_apply_requires_acknowledgement(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("models:\n  fast:\n    provider: external\n    model: small\n")
    with pytest.raises(SystemExit):
        main(["calibrate", "--config", str(config), "--apply"])
    assert config.read_text().startswith("models:")


def test_bootstrap_deterministic_and_opt_in(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "db.sqlite3")
    original = confidence_report(ledger, tmp_path)
    assert "bootstrap_uncertainty" not in original
    report = confidence_report(ledger, tmp_path, bootstrap_samples=100)
    assert report["bootstrap_uncertainty"]["sample_size"] == 0
    with pytest.raises(Exception, match="bootstrap_samples"):
        confidence_report(ledger, tmp_path, bootstrap_samples=10001)
    obs = [
        ConfidenceObservation(
            str(n),
            p,
            "valid",
            "local_code",
            "model",
            "example",
            "standard",
            "coding",
            "verified-success" if n % 2 else "verified-failure",
        )
        for n, p in enumerate([0.2, 0.5, 0.8, 0.9])
    ]
    assert _bootstrap_intervals(obs, 200) == _bootstrap_intervals(obs, 200)


def test_replace_rejects_locator_conflicts_and_attempt_scoped_measurements(
    tmp_path: Path,
) -> None:
    ledger = OutcomeLedger(tmp_path / "db.sqlite3")
    profile = TaskProfiler(default_config().intelligence).profile("Fix test")
    task = ledger.create_pending("Fix test", tmp_path, profile)
    other_task = ledger.create_pending("Fix other test", tmp_path, profile)
    repo_id = ledger.repository_id(tmp_path)
    attempt, linked, _ = ledger.link_agent_session_for_task(
        task,
        agent="claude-code",
        external_session_id="session-1",
        content_hash="a" * 64,
        session_path=str(tmp_path / "first.jsonl"),
        expected_repository_id=repo_id,
    )
    other_attempt, _, _ = ledger.link_agent_session_for_task(
        other_task,
        agent="claude-code",
        external_session_id="session-2",
        content_hash="b" * 64,
        session_path=str(tmp_path / "second.jsonl"),
        expected_repository_id=repo_id,
    )
    with pytest.raises(ConflictingSessionLinkError, match="another link"):
        ledger.replace_unmeasured_session(
            task,
            attempt_id=attempt.attempt_id,
            agent="claude-code",
            external_session_id="session-1",
            content_hash="c" * 64,
            session_path=str(tmp_path / "second.jsonl"),
            expected_repository_id=repo_id,
        )
    assert ledger.agent_sessions(attempt.attempt_id)[0].content_hash == "a" * 64

    with pytest.raises(CrossRepositoryError, match="mismatch"):
        ledger.replace_unmeasured_session(
            task,
            attempt_id=other_attempt.attempt_id,
            agent="claude-code",
            external_session_id="session-1",
            content_hash="c" * 64,
            session_path=str(tmp_path / "first.jsonl"),
            expected_repository_id=repo_id,
        )

    ledger.record_ter_measurement(
        attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version="3.0.0",
        metrics={"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 1},
        session_link_id=None,
    )
    with pytest.raises(ConflictingSessionLinkError, match="Measured"):
        ledger.replace_unmeasured_session(
            task,
            attempt_id=attempt.attempt_id,
            agent="claude-code",
            external_session_id="session-1",
            content_hash="d" * 64,
            session_path=str(tmp_path / "first.jsonl"),
            expected_repository_id=repo_id,
        )
    assert ledger.agent_sessions(attempt.attempt_id)[0].session_link_id == (linked.session_link_id)


def test_link_session_to_task_replace_requires_attempt_and_refreshes_hash(
    tmp_path: Path,
) -> None:
    ledger = OutcomeLedger(tmp_path / "db.sqlite3")
    profile = TaskProfiler(default_config().intelligence).profile("Fix test")
    task = ledger.create_pending("Fix test", tmp_path, profile)
    repo_id = ledger.repository_id(tmp_path)
    attempt, _, _ = ledger.link_agent_session_for_task(
        task,
        agent="claude-code",
        external_session_id="session-1",
        content_hash="a" * 64,
        session_path=str(tmp_path / "first.jsonl"),
        expected_repository_id=repo_id,
    )
    session = tmp_path / "session.jsonl"
    session.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "user",
                    "sessionId": "session-1",
                    "message": {"role": "user", "content": "Please fix"},
                },
                {
                    "type": "assistant",
                    "sessionId": "session-1",
                    "message": {"role": "assistant", "content": "Fixed"},
                },
            )
        )
        + "\n"
    )
    with pytest.raises(Exception, match="requires --attempt"):
        link_session_to_task(
            ledger,
            task,
            repository_root=tmp_path,
            session_path=session,
            agent="claude-code",
            replace_unmeasured=True,
        )
    result = link_session_to_task(
        ledger,
        task,
        repository_root=tmp_path,
        session_path=session,
        agent="claude-code",
        attempt_id=attempt.attempt_id,
        replace_unmeasured=True,
    )
    assert result.created is False
    link = ledger.agent_sessions(attempt.attempt_id)[0]
    assert result.link.session_link_id == link.session_link_id
    assert link.content_hash != "a" * 64


def test_codex_measurement_without_ter_loader_fails_closed(tmp_path: Path) -> None:
    ledger = OutcomeLedger(tmp_path / "db.sqlite3")
    profile = TaskProfiler(default_config().intelligence).profile("Fix test")
    task = ledger.create_pending("Fix test", tmp_path, profile)
    rollout = tmp_path / "rollout-session.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "session_meta", "payload": {"id": "session-1"}},
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Fix it"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "Fixed"},
                },
            )
        )
        + "\n"
    )
    link_session_to_task(
        ledger,
        task,
        repository_root=tmp_path,
        session_path=rollout,
        agent="codex",
    )
    with pytest.raises(LinkedEfficiencyError, match="validated Codex loader"):
        measure_linked_sessions(ledger, tmp_path)
