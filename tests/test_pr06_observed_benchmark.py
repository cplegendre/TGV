from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgv.bench.observed import (
    ObservedBenchmarkError,
    _evidence_sufficiency,
    observed_benchmark,
)
from tgv.cli import main
from tgv.config import default_config
from tgv.exceptions import LedgerReadError
from tgv.intelligence import TaskProfiler
from tgv.models import CapabilityTier, Route
from tgv.outcomes import OutcomeLedger
from tgv.service import TGVService
from tgv.session_linking import link_session_to_task

SESSION_ID = "8f3c4e5a-2b7d-4c1e-9a6f-1d2e3f4a5b6c"
OTHER_SESSION_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
THIRD_SESSION_ID = "2c3d4e5f-6a7b-8c9d-0e1f-2a3b4c5d6e7f"
SECRET_TEXT = "SECRET-OBSERVED-SESSION-CONTENT"


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
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _ledger(repo: Path) -> OutcomeLedger:
    return OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


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


def _measurement(
    repo: Path,
    result,
    *,
    ter: float = 0.8,
    aligned: int = 80,
    waste: int = 20,
    analyzer_version: str = "3.0.0",
    measured_at: datetime | None = None,
):
    return _ledger(repo).record_ter_measurement(
        result.attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version=analyzer_version,
        metrics={
            "aggregate_ter": ter,
            "aligned_tokens": aligned,
            "waste_tokens": waste,
            "total_tokens": aligned + waste,
        },
        session_link_id=result.link.session_link_id,
        measured_at=measured_at or datetime(2026, 1, 1, tzinfo=UTC),
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


def test_empty_ledger_reports_unavailable_coverage(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = observed_benchmark(_ledger(repo), repo)

    assert report["status"] == "empty"
    assert report["coverage"]["eligible_task_count"] == 0
    assert report["coverage"]["linked_session_count"] == 0
    assert report["coverage"]["measurement_coverage"] is None
    assert report["coverage"]["verified_success_rate"] is None
    assert report["groups"] == {
        "by_route": {},
        "by_model": {},
        "by_capability_tier": {},
        "by_task_category": {},
    }
    assert report["observations"] == []


def test_tasks_without_sessions_keep_outcome_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    _complete(repo, task_id)

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]
    assert coverage["eligible_task_count"] == 1
    assert coverage["observation_count"] == 0
    assert coverage["linked_session_count"] == 0
    assert coverage["measurement_coverage"] is None
    assert coverage["verified_outcome_count"] == 1
    assert coverage["verified_success_count"] == 1
    assert report["status"] == "insufficient-data"


def test_linked_sessions_without_measurements_are_missing_not_zero(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    _link(repo, task_id, _session(tmp_path / "session.jsonl"))

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]
    assert coverage["linked_session_count"] == 1
    assert coverage["measured_session_count"] == 0
    assert coverage["missing_measurement_sessions"] == 1
    assert coverage["measurement_coverage"] == 0.0
    assert coverage["aggregate_ter"] is None
    observation = report["observations"][0]
    assert observation["measurement_status"] == "missing"
    assert observation["measurement_id"] is None
    assert observation["analyzer"] is None
    assert observation["analyzer_version"] is None


def test_valid_measured_sessions_and_all_group_dimensions(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first_task = _task(repo, request="fix the parser bug", model="model-a")
    second_task = _task(repo, request="fix another parser bug", model="model-a")
    first = _link(repo, first_task, _session(tmp_path / "first.jsonl"))
    second = _link(
        repo,
        second_task,
        _session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID),
    )
    _measurement(repo, first, ter=0.8, aligned=80, waste=20)
    _measurement(repo, second, ter=0.5, aligned=50, waste=50)
    _complete(repo, first_task)
    _complete(repo, second_task, success=False)

    report = observed_benchmark(_ledger(repo), repo)
    route = report["groups"]["by_route"]["local_code"]
    assert route["eligible_task_count"] == 2
    assert route["measured_session_count"] == 2
    assert route["aggregate_ter"] == 0.65
    assert route["aligned_tokens"] == 130
    assert route["waste_tokens"] == 70
    assert route["total_tokens"] == 200
    assert report["groups"]["by_model"]["model-a"] == route
    assert report["groups"]["by_capability_tier"]["standard"] == route
    assert report["groups"]["by_task_category"]["bug_fix"] == route
    assert report["status"] == "observational"


def test_scoreboard_advanced_route_uses_persisted_local_code(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(
        repo,
        tier=CapabilityTier.ADVANCED,
        model="advanced-model",
        route=Route.LOCAL_CODE,
    )
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    _measurement(repo, result)

    report = observed_benchmark(_ledger(repo), repo)
    assert report["groups"]["by_route"]["local_code"]["measured_session_count"] == 1
    assert report["groups"]["by_capability_tier"]["advanced"]["measured_session_count"] == 1
    assert report["observations"][0]["route"] == "local_code"


def test_deterministic_advanced_route_uses_persisted_frontier_standard(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(
        repo,
        tier=CapabilityTier.ADVANCED,
        model="frontier-standard-model",
        route=Route.FRONTIER_STANDARD,
    )
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    _measurement(repo, result)

    report = observed_benchmark(_ledger(repo), repo)
    assert report["groups"]["by_route"]["frontier_standard"]["measured_session_count"] == 1
    assert "local_code" not in report["groups"]["by_route"]
    assert report["observations"][0]["route"] == "frontier_standard"


def test_historical_advanced_route_is_unavailable_not_inferred(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo, tier=CapabilityTier.ADVANCED, model="advanced-model")
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE outcomes SET metadata = '{}' WHERE task_id = ?", (task_id,)
        )
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    _measurement(repo, result)

    report = observed_benchmark(_ledger(repo), repo)
    assert report["groups"]["by_route"]["unavailable"]["measured_session_count"] == 1
    assert "local_code" not in report["groups"]["by_route"]
    assert report["observations"][0]["route"] == "unavailable"


def test_outcome_categories_and_verified_success_denominator(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    success = _task(repo)
    failure = _task(repo, request="fix another helper bug")
    unverified = _task(repo, request="fix third helper bug")
    incomplete = _task(repo, request="fix fourth helper bug")
    sessions = [
        _session(tmp_path / "outcome-1.jsonl"),
        _session(tmp_path / "outcome-2.jsonl", session_id=OTHER_SESSION_ID),
        _session(tmp_path / "outcome-3.jsonl", session_id=THIRD_SESSION_ID),
        _session(
            tmp_path / "outcome-4.jsonl",
            session_id="3d4e5f6a-7b8c-9d0e-1f2a-3b4c5d6e7f8a",
        ),
    ]
    for task_id, session in zip((success, failure, unverified, incomplete), sessions, strict=True):
        result = _link(repo, task_id, session)
        _measurement(repo, result)
    _complete(repo, success)
    _complete(repo, failure, success=False)
    _ledger(repo).record_execution(unverified, status="completed")

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]
    assert coverage["eligible_task_count"] == 4
    assert coverage["verified_outcome_count"] == 2
    assert coverage["verified_success_count"] == 1
    assert coverage["verified_failure_count"] == 1
    assert coverage["unverified_outcome_count"] == 1
    assert coverage["incomplete_task_count"] == 1
    assert coverage["verified_success_rate"] == 0.5
    interval = coverage["verified_success_confidence_interval_95"]
    assert interval is not None and interval[0] < 0.5 < interval[1]


def test_wilson_interval_is_clipped_at_boundaries(tmp_path: Path) -> None:
    for success, expected_boundary in ((True, 1.0), (False, 0.0)):
        case_path = tmp_path / f"wilson-{int(success)}"
        case_path.mkdir()
        repo = _repo(case_path)
        task_id = _task(repo)
        result = _link(repo, task_id, _session(case_path / "session.jsonl"))
        _measurement(repo, result)
        _complete(repo, task_id, success=success)
        report = observed_benchmark(_ledger(repo), repo)
        interval = report["coverage"]["verified_success_confidence_interval_95"]
        assert interval is not None
        assert expected_boundary in interval


def test_multiple_attempts_do_not_inflate_task_outcomes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    first = _link(repo, task_id, _session(tmp_path / "attempt-1.jsonl"))
    second_session = _session(tmp_path / "attempt-2.jsonl", session_id=OTHER_SESSION_ID)
    second_attempt = _ledger(repo).create_execution_attempt(task_id)
    second = _ledger(repo).link_agent_session(
        second_attempt.attempt_id,
        agent="claude-code",
        external_session_id=OTHER_SESSION_ID,
        content_hash=hashlib.sha256(second_session.read_bytes()).hexdigest(),
        session_path=str(second_session),
    )
    _measurement(repo, first, ter=0.8, aligned=80, waste=20)
    _ledger(repo).record_ter_measurement(
        second_attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version="3.0.0",
        metrics={
            "aggregate_ter": 0.5,
            "aligned_tokens": 50,
            "waste_tokens": 50,
            "total_tokens": 100,
        },
        session_link_id=second.session_link_id,
        measured_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    _complete(repo, task_id)

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]
    assert coverage["eligible_task_count"] == 1
    assert coverage["linked_task_count"] == 1
    assert coverage["observation_count"] == 2
    assert coverage["attempt_count"] == 2
    assert coverage["linked_session_count"] == 2
    assert coverage["measured_session_count"] == 2
    assert coverage["verified_success_count"] == 1
    assert report["groups"]["by_route"]["local_code"]["eligible_task_count"] == 1
    assert len(report["observations"]) == 2


def test_multiple_analyzer_versions_use_canonical_and_scoped_reporting(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    older = _measurement(
        repo,
        result,
        ter=0.8,
        aligned=80,
        waste=20,
        analyzer_version="3.0.0",
        measured_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    latest = _measurement(
        repo,
        result,
        ter=0.5,
        aligned=50,
        waste=50,
        analyzer_version="3.0.1",
        measured_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    second_task = _task(repo, request="fix another helper bug")
    second = _link(
        repo,
        second_task,
        _session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID),
    )
    _measurement(
        repo,
        second,
        ter=0.9,
        aligned=90,
        waste=10,
        analyzer_version="3.0.0",
        measured_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]
    assert coverage["measured_session_count"] == 2
    assert coverage["selected_analyzer_version"] == "3.0.0"
    assert coverage["aggregate_ter"] == 0.9
    assert coverage["total_tokens"] == 100
    assert coverage["mixed_analyzer_versions"] is True
    assert {scope["analyzer_version"] for scope in coverage["analyzer_scopes"]} == {
        "3.0.0",
        "3.0.1",
    }
    observation = next(item for item in report["observations"] if item["task_id"] == task_id)
    assert observation["measurement_id"] == latest.measurement_id
    assert observation["measurement_id"] != older.measurement_id


def test_analyzer_scope_is_selected_globally_across_groups(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    local_task = _task(repo, request="fix local helper bug")
    frontier_task = _task(
        repo,
        request="implement a large documented feature",
        tier=CapabilityTier.ADVANCED,
        model="frontier-standard-model",
        route=Route.FRONTIER_STANDARD,
    )
    other_local_task = _task(repo, request="fix another local helper bug")
    local = _link(repo, local_task, _session(tmp_path / "local.jsonl"))
    frontier = _link(
        repo,
        frontier_task,
        _session(
            tmp_path / "frontier.jsonl", session_id=OTHER_SESSION_ID
        ),
    )
    other_local = _link(
        repo,
        other_local_task,
        _session(tmp_path / "other-local.jsonl", session_id=THIRD_SESSION_ID),
    )
    _measurement(repo, local, analyzer_version="3.0.0")
    _measurement(repo, frontier, analyzer_version="3.0.1", ter=0.2, aligned=20, waste=80)
    _measurement(repo, other_local, analyzer_version="3.0.0", ter=0.5, aligned=50, waste=50)

    report = observed_benchmark(_ledger(repo), repo)

    assert report["coverage"]["selected_analyzer_version"] == "3.0.0"
    assert report["coverage"]["aggregate_ter"] == 0.65
    assert report["groups"]["by_route"]["local_code"]["aggregate_ter"] == 0.65
    assert report["groups"]["by_route"]["frontier_standard"]["aggregate_ter"] is None
    assert report["groups"]["by_route"]["frontier_standard"]["measured_session_count"] == 1
    assert report["groups"]["by_route"]["frontier_standard"]["aggregated_session_count"] == 0
    excluded = report["groups"]["by_route"]["frontier_standard"]
    assert excluded["excluded_from_aggregate_sessions"] == 1
    observation_by_route = {
        item["route"]: item["aggregated_in_report"] for item in report["observations"]
    }
    assert observation_by_route["local_code"] is True
    assert observation_by_route["frontier_standard"] is False


def test_evidence_sufficiency_uses_selected_analyzer_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(20):
        analyzer_version = "3.0.0" if index < 10 else "3.0.1"
        task_id = _task(repo, request=f"fix analyzer-scope bug {index}")
        result = _link(
            repo,
            task_id,
            _session(
                tmp_path / f"analyzer-scope-{index}.jsonl",
                session_id=f"analyzer-scope-{index}",
            ),
        )
        _measurement(
            repo,
            result,
            ter=0.8 if analyzer_version == "3.0.0" else 0.2,
            aligned=80,
            waste=20,
            analyzer_version=analyzer_version,
        )
        _complete(repo, task_id)

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]

    assert coverage["measured_session_count"] == 20
    assert coverage["aggregated_session_count"] == 10
    assert coverage["selected_analyzer_version"] == "3.0.0"
    assert coverage["aggregate_ter"] == 0.8
    assert coverage["total_tokens"] == 1000
    assert coverage["evidence_sufficiency"]["status"] == "limited"
    assert coverage["evidence_sufficiency"]["aggregated_session_sample_size"] == 10
    assert coverage["evidence_sufficiency"]["reasons"] == [
        "sample size is below the conservative minimum"
    ]


def test_zero_token_measurement_preserves_pr05_semantics(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    _measurement(repo, result, ter=1.0, aligned=0, waste=0)
    _complete(repo, task_id)

    report = observed_benchmark(_ledger(repo), repo)
    assert report["coverage"]["measured_session_count"] == 1
    assert report["coverage"]["aggregate_ter"] == 1.0
    assert report["coverage"]["total_tokens"] == 0
    assert report["observations"][0]["metrics"]["total_tokens"] == 0


def test_failed_attempt_followed_by_successful_retry_remains_one_verified_task(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    first = _link(repo, task_id, _session(tmp_path / "first.jsonl"))
    _ledger(repo).update_execution_attempt(
        first.attempt.attempt_id, status="failed"
    )
    second = _link(
        repo,
        task_id,
        _session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID),
    )
    _ledger(repo).update_execution_attempt(
        second.attempt.attempt_id, status="completed"
    )
    _measurement(repo, first)
    _measurement(repo, second, ter=0.5, aligned=50, waste=50)
    _complete(repo, task_id)

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]

    assert coverage["eligible_task_count"] == 1
    assert coverage["attempt_count"] == 2
    assert coverage["attempt_status_counts"] == {"completed": 1, "failed": 1}
    assert coverage["verified_success_count"] == 1
    assert coverage["verified_failure_count"] == 0
    assert {
        (item["attempt_id"], item["attempt_status"])
        for item in report["observations"]
    } == {
        (first.attempt.attempt_id, "failed"),
        (second.attempt.attempt_id, "completed"),
    }


def test_evidence_sufficiency_distinguishes_missing_limited_and_sufficient_samples() -> None:
    assert _evidence_sufficiency(0, 0)["status"] == "missing"
    assert _evidence_sufficiency(1, 1)["status"] == "limited"
    assert _evidence_sufficiency(19, 20)["status"] == "limited"
    sufficient = _evidence_sufficiency(20, 20)
    assert sufficient["status"] == "sufficient"
    assert sufficient["reasons"] == []
    assert sufficient["aggregated_session_sample_size"] == 20
    assert _evidence_sufficiency(20, 0)["reasons"] == ["missing verified outcomes"]
    assert _evidence_sufficiency(0, 20)["reasons"] == ["missing comparable TER measurements"]


def test_evidence_sufficiency_reaches_threshold_with_comparable_sessions(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    for index in range(20):
        task_id = _task(repo, request=f"fix sufficient-evidence bug {index}")
        result = _link(
            repo,
            task_id,
            _session(
                tmp_path / f"sufficient-{index}.jsonl",
                session_id=f"sufficient-{index}",
            ),
        )
        _measurement(repo, result)
        _complete(repo, task_id)

    coverage = observed_benchmark(_ledger(repo), repo)["coverage"]

    assert coverage["aggregated_session_count"] == 20
    assert coverage["evidence_sufficiency"]["status"] == "sufficient"
    assert coverage["evidence_sufficiency"]["aggregated_session_sample_size"] == 20
    assert coverage["evidence_sufficiency"]["reasons"] == []


def test_malformed_measurements_are_invalid_not_missing_efficiency(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    measurement = _measurement(repo, result)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE ter_measurements SET metrics = ? WHERE measurement_id = ?",
            (json.dumps({"aggregate_ter": None}), measurement.measurement_id),
        )
    unlinked_task = _task(repo, request="fix another helper bug")
    unlinked_attempt = _ledger(repo).create_execution_attempt(unlinked_task)
    _ledger(repo).record_ter_measurement(
        unlinked_attempt.attempt_id,
        analyzer="TER CLI",
        analyzer_version="3.0.0",
        metrics={"aggregate_ter": 0.5, "aligned_tokens": 1, "waste_tokens": 1},
        session_link_id=None,
    )

    report = observed_benchmark(_ledger(repo), repo)
    coverage = report["coverage"]
    assert coverage["measured_session_count"] == 0
    assert coverage["missing_measurement_sessions"] == 1
    assert coverage["invalid_measurement_count"] == 2
    assert coverage["unattributed_invalid_measurements"] == 1
    assert coverage["sessions_with_invalid_measurements"] == 1
    assert coverage["measurement_coverage"] == 0.0
    assert coverage["aggregate_ter"] is None
    assert report["observations"][0]["measurement_status"] == "missing"


def test_invalid_measurement_association_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first_task = _task(repo)
    second_task = _task(repo, request="fix another helper bug")
    first = _link(repo, first_task, _session(tmp_path / "assoc-1.jsonl"))
    second = _link(
        repo,
        second_task,
        _session(tmp_path / "assoc-2.jsonl", session_id=OTHER_SESSION_ID),
    )
    measurement = _measurement(repo, first)
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        connection.execute(
            "UPDATE ter_measurements SET session_link_id = ? WHERE measurement_id = ?",
            (second.link.session_link_id, measurement.measurement_id),
        )

    with pytest.raises(ObservedBenchmarkError, match="invalid outcome association"):
        observed_benchmark(_ledger(repo), repo)


def test_deterministic_json_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    _measurement(repo, result)
    _complete(repo, task_id)

    first = json.dumps(
        observed_benchmark(_ledger(repo), repo), sort_keys=True, separators=(",", ":")
    )
    second = json.dumps(
        observed_benchmark(_ledger(repo), repo), sort_keys=True, separators=(",", ":")
    )
    assert first == second


def test_cli_json_and_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl", text=SECRET_TEXT)
    result = _link(repo, task_id, session)
    _measurement(repo, result)
    _complete(repo, task_id)

    main(["benchmark", "observed", "--repo", str(repo), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "observed-ledger"
    assert report["coverage"]["measured_session_count"] == 1
    assert SECRET_TEXT not in json.dumps(report)
    assert str(session) not in json.dumps(report)

    main(["benchmark", "observed", "--repo", str(repo)])
    text = capsys.readouterr().out
    assert "source: observed-ledger" in text
    assert "by_route:local_code" in text
    assert SECRET_TEXT not in text
    assert str(session) not in text


def test_service_prepare_persists_authoritative_frontier_standard_route(
    tmp_path: Path,
) -> None:
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
    assert task.metadata["authoritative_route"] == "frontier_standard"
    assert bundle.decision.route.value == "frontier_standard"
    assert task.selected_capability_tier is CapabilityTier.ADVANCED


def test_cli_rejects_enforce_for_observed_benchmark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SystemExit) as exc_info:
        main(["benchmark", "observed", "--repo", str(repo), "--enforce"])

    assert exc_info.value.code == 2
    assert "does not support --enforce" in capsys.readouterr().err


def test_cli_rejects_irrelevant_benchmark_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "benchmark",
                "observed",
                "--repo",
                str(repo),
                "--cases",
                str(tmp_path / "cases.jsonl"),
                "--limit",
                "10",
            ]
        )

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "does not support --cases, --limit" in error


def test_legacy_outcome_benchmark_remains_compatible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    main(["benchmark", "outcomes", "--repo", str(repo), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["release_gate"]["passed"] is False
    assert report["overall"]["total"] == 0


def test_privacy_and_read_only_ledger_use(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    session = _session(tmp_path / "session.jsonl", text=SECRET_TEXT)
    result = _link(repo, task_id, session)
    _measurement(repo, result)
    _complete(repo, task_id)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        before = "\n".join(connection.iterdump())

    report = observed_benchmark(_ledger(repo), repo)
    serialized = json.dumps(report, sort_keys=True)
    with sqlite3.connect(database_path) as connection:
        after = "\n".join(connection.iterdump())

    assert SECRET_TEXT not in serialized
    assert str(session) not in serialized
    assert before == after


def test_missing_ledger_returns_empty_report_without_creating_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    ledger = OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3", read_only=True)

    report = observed_benchmark(ledger, repo)
    main(["benchmark", "observed", "--repo", str(repo), "--json"])

    assert report["status"] == "empty"
    assert json.loads(capsys.readouterr().out)["status"] == "empty"
    assert not (repo / ".tgv").exists()


def test_read_only_ledger_rejects_direct_access_to_missing_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ledger = OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3", read_only=True)

    with pytest.raises(LedgerReadError, match="does not exist"):
        ledger.query()

    assert not (repo / ".tgv").exists()


def test_persisting_route_rejects_invalid_or_missing_task_metadata(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE outcomes SET metadata = '[]' WHERE task_id = ?", (task_id,))

    profile = TaskProfiler(default_config().intelligence).profile("fix helper bug")
    with pytest.raises(ValueError, match="invalid metadata"):
        _ledger(repo).record_routing(
            task_id,
            revised_profile=profile,
            confidence=0.8,
            model="local-code",
            capability_tier=CapabilityTier.STANDARD,
            reasons=["test"],
            files=[],
            symbols=[],
            context_token_estimate=10,
            route=Route.LOCAL_CODE,
        )
    with pytest.raises(KeyError, match="Unknown task id"):
        _ledger(repo).record_routing(
            "missing-task",
            revised_profile=profile,
            confidence=0.8,
            model="local-code",
            capability_tier=CapabilityTier.STANDARD,
            reasons=["test"],
            files=[],
            symbols=[],
            context_token_estimate=10,
            route=Route.LOCAL_CODE,
        )


def test_invalid_persisted_route_is_reported_explicitly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE outcomes SET metadata = ? WHERE task_id = ?",
            (json.dumps({"authoritative_route": "not-a-route"}), task_id),
        )
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    _measurement(repo, result)

    report = observed_benchmark(_ledger(repo), repo)

    assert report["groups"]["by_route"]["invalid-route"]["measured_session_count"] == 1
    assert report["observations"][0]["route"] == "invalid-route"


def test_existing_v3_ledger_is_opened_without_database_or_new_sidecar_files(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    result = _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    _measurement(repo, result)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True):
        pass
    sidecars_before = {
        path.name
        for path in database_path.parent.iterdir()
        if path.name.startswith(database_path.name + "-")
    }
    before = database_path.read_bytes()

    report = observed_benchmark(
        OutcomeLedger(database_path, read_only=True), repo
    )

    assert report["coverage"]["measured_session_count"] == 1
    assert database_path.read_bytes() == before
    sidecars_after = {
        path.name
        for path in database_path.parent.iterdir()
        if path.name.startswith(database_path.name + "-")
    }
    assert sidecars_after == sidecars_before


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_ledgers_are_rejected_without_migration(tmp_path: Path, version: int) -> None:
    repo = _repo(tmp_path)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("CREATE TABLE outcomes (task_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    before = database_path.read_bytes()

    with pytest.raises(LedgerReadError, match="cannot be opened read-only"):
        observed_benchmark(OutcomeLedger(database_path, read_only=True), repo)

    assert database_path.read_bytes() == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == version


def test_malformed_database_is_rejected_without_modification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"not a sqlite database")
    before = database_path.read_bytes()

    with pytest.raises(LedgerReadError, match="cannot be opened read-only"):
        observed_benchmark(OutcomeLedger(database_path, read_only=True), repo)

    assert database_path.read_bytes() == before


def test_v2_ledger_is_not_migrated_by_observed_benchmark(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE session_locators")
        connection.execute("UPDATE schema_version SET version = 2")

    with pytest.raises(LedgerReadError, match="cannot be opened read-only"):
        observed_benchmark(OutcomeLedger(database_path, read_only=True), repo)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2


def test_invalid_schema_version_is_an_actionable_read_only_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    task_id = _task(repo)
    _link(repo, task_id, _session(tmp_path / "session.jsonl"))
    database_path = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_version SET version = 'bogus'")
    before = database_path.read_bytes()

    with pytest.raises(LedgerReadError, match="invalid schema version value"):
        observed_benchmark(OutcomeLedger(database_path, read_only=True), repo)

    with pytest.raises(SystemExit) as exc_info:
        main(["benchmark", "observed", "--repo", str(repo), "--json"])

    assert exc_info.value.code == 2
    assert "invalid schema version value" in capsys.readouterr().err
    assert database_path.read_bytes() == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == "bogus"
