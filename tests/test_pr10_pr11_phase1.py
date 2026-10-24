"""Phase 1 regression coverage: measurement integrity and attempt lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgv.config import default_config
from tgv.efficiency import TERAnalysis
from tgv.exceptions import ConflictingSessionLinkError, ValidationFailure
from tgv.intelligence import TaskProfiler
from tgv.linked_efficiency import LinkedEfficiencyError, _aggregate, measure_linked_sessions
from tgv.outcomes import OutcomeLedger
from tgv.session_linking import link_session_to_task, validate_agent_session


def _task(repo: Path) -> tuple[OutcomeLedger, str]:
    ledger = OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")
    profile = TaskProfiler(default_config().intelligence).profile("Fix a bug")
    return ledger, ledger.create_pending("Fix a bug", repo, profile)


def _session(path: Path, session_id: str, *, hollow: bool = False) -> Path:
    records = [
        {
            "type": typ,
            "sessionId": session_id,
            "message": {"role": typ, "content": "" if hollow else text},
        }
        for typ, text in [("user", "Please fix the bug"), ("assistant", "Fixed the bug")]
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    return path


def test_hollow_session_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailure, match="non-empty user and assistant"):
        validate_agent_session(
            _session(tmp_path / "empty.jsonl", "sid-a", hollow=True), "claude-code"
        )


def test_zero_token_ter_does_not_persist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ledger, task_id = _task(repo)
    result = link_session_to_task(
        ledger,
        task_id,
        repository_root=repo,
        session_path=_session(tmp_path / "valid.jsonl", "sid-a"),
        agent="claude-code",
    )

    def zero(_path: Path) -> TERAnalysis:
        return TERAnalysis("sid-a", 1.0, 0, 0, "fake-ter", "test", 0)

    with pytest.raises(LinkedEfficiencyError, match="zero tokens"):
        measure_linked_sessions(ledger, repo, analyzer=zero)
    assert ledger.ter_measurements(result.attempt.attempt_id) == []


def test_second_session_explicit_attempt_rejected_at_link_time(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ledger, task_id = _task(repo)
    first = link_session_to_task(
        ledger,
        task_id,
        repository_root=repo,
        session_path=_session(tmp_path / "first.jsonl", "sid-a"),
        agent="claude-code",
    )
    with pytest.raises(ConflictingSessionLinkError, match="already has a linked session"):
        link_session_to_task(
            ledger,
            task_id,
            repository_root=repo,
            session_path=_session(tmp_path / "second.jsonl", "sid-b"),
            agent="claude-code",
            attempt_id=first.attempt.attempt_id,
        )
    assert len(ledger.agent_sessions(first.attempt.attempt_id)) == 1


def test_completion_closes_only_pending_attempts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ledger, task_id = _task(repo)
    pending = ledger.create_execution_attempt(task_id)
    failed = ledger.create_execution_attempt(task_id, status="failed")
    assert ledger.complete_pending_attempts(task_id) == 1
    assert ledger.complete_pending_attempts(task_id) == 0
    statuses = {a.attempt_id: a for a in ledger.execution_attempts(task_id)}
    assert statuses[pending.attempt_id].status == "completed"
    assert statuses[pending.attempt_id].completed_at is not None
    assert statuses[failed.attempt_id].status == "failed"


def test_empty_aggregation_is_not_perfect_efficiency() -> None:
    result = _aggregate([])
    assert result["aggregate_ter"] is None
    assert result["efficiency_status"] == "insufficient-token-evidence"
