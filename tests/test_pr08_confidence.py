from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml

from tgv import confidence
from tgv.bench.observed import _wilson_interval
from tgv.cli import main
from tgv.confidence import (
    ConfidenceDiagnosticError,
    _confidence_value,
    confidence_report,
    render_confidence_report,
)
from tgv.config import default_config
from tgv.intelligence import TaskProfiler
from tgv.models import CapabilityTier, Route, RouteDecision, ScoreboardRecommendation
from tgv.outcomes import OutcomeLedger
from tgv.router import Router
from tgv.service import TGVService

SECRET = "PR08-private-prompt-response-transcript-classified-span"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _ledger(repo: Path) -> OutcomeLedger:
    return OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3", retain_raw_prompts=True)


def _task(
    repo: Path,
    value: Any = 0.8,
    outcome: bool | None = True,
    *,
    status: str = "completed",
    route: Any = "local_code",
    source: Any = "model",
    model: str | None = "code-model",
    tier: CapabilityTier | None = CapabilityTier.STANDARD,
    metadata: Any = None,
) -> str:
    ledger = _ledger(repo)
    profile = TaskProfiler(default_config().intelligence).profile("Fix helper")
    task_id = ledger.create_pending(SECRET, repo, profile)
    ledger.record_routing(
        task_id,
        revised_profile=profile,
        confidence=0.8,
        model="code-model",
        capability_tier=CapabilityTier.STANDARD,
        reasons=[SECRET],
        files=[SECRET],
        symbols=[],
        context_token_estimate=10,
        route=Route.LOCAL_CODE,
        decision_source="model",
    )
    ledger.record_execution(task_id, status=status)
    if outcome is not None:
        ledger.record_validation(task_id, tests_passed=outcome, static_checks_passed=None)
    if metadata is None:
        metadata = {
            "authoritative_route": route,
            "routing_decision_source": source,
            "private": SECRET,
        }
    with closing(sqlite3.connect(ledger.path)) as connection, connection:
        connection.execute(
            "UPDATE outcomes SET routing_confidence=?, selected_model=?, "
            "selected_capability_tier=?, metadata=? WHERE task_id=?",
            (value, model, tier.value if tier else None, json.dumps(metadata), task_id),
        )
    return task_id


def _report(repo: Path) -> dict[str, Any]:
    # Passing a writable ledger must still use only the read-only connection.
    return confidence_report(_ledger(repo), repo)


def _bins(repo: Path) -> list[dict[str, Any]]:
    return _report(repo)["overall"]["reliability_bins"]


def test_missing_ledger_is_empty_without_creating_any_files(repo: Path) -> None:
    report = _report(repo)
    overall = report["overall"]
    assert overall["total_tasks"] == overall["scored_observations"] == 0
    for key in (
        "brier_score",
        "expected_calibration_error",
        "verification_coverage",
        "confidence_coverage",
        "scoring_coverage",
        "verified_success_rate",
        "verified_success_interval_95",
    ):
        assert overall[key] is None
    assert overall["evidence_sufficiency"]["status"] == "missing"
    assert all(groups == {} for groups in report["groups"].values())
    assert list(repo.iterdir()) == []


def test_existing_empty_ledger_and_empty_bins(repo: Path) -> None:
    _ledger(repo).query()
    database = _ledger(repo).path
    before = database.read_bytes()
    report = _report(repo)
    assert report["overall"]["total_tasks"] == 0
    assert database.read_bytes() == before
    for index, item in enumerate(report["overall"]["reliability_bins"]):
        assert item == {
            "lower_bound": index / 10,
            "upper_bound": (index + 1) / 10,
            "upper_inclusive": index == 9,
            "count": 0,
            "mean_confidence": None,
            "verified_success_rate": None,
            "wilson_interval_95": None,
            "calibration_gap": None,
        }


@pytest.mark.parametrize(
    ("values", "outcomes", "brier", "ece"),
    [
        ([0.0, 1.0], [False, True], 0.0, 0.0),
        ([0.9, 0.9], [True, False], 0.41, 0.4),
        ([0.2, 0.2], [True, False], 0.34, 0.3),
        ([0.8, 0.8], [True, True], 0.04, 0.2),
        ([0.8, 0.8], [False, False], 0.64, 0.8),
        ([0.2, 0.8, 0.7, 0.1], [True, False, True, False], 0.345, 0.5),
        ([0.5, 0.5], [True, False], 0.25, 0.0),
        ([0.0, 1.0], [True, False], 1.0, 1.0),
    ],
)
def test_ledger_backed_brier_and_ece(
    repo: Path,
    values: list[float],
    outcomes: list[bool],
    brier: float,
    ece: float,
) -> None:
    for value, outcome in zip(values, outcomes, strict=True):
        _task(repo, value, outcome)
    metrics = _report(repo)["overall"]
    assert metrics["brier_score"] == pytest.approx(brier)
    assert metrics["expected_calibration_error"] == pytest.approx(ece)
    assert metrics["verified_successes"] == sum(outcomes)
    assert metrics["verified_failures"] == len(outcomes) - sum(outcomes)
    assert metrics["scored_observations"] == len(outcomes)
    assert metrics["evidence_sufficiency"]["status"] == "limited"


def test_ece_uses_weights_and_unrounded_bin_means(repo: Path) -> None:
    for value, outcome in [(0.11, True), (0.12, False), (0.13, False), (0.95, True)]:
        _task(repo, value, outcome)
    metrics = _report(repo)["overall"]
    assert metrics["expected_calibration_error"] == pytest.approx(0.1725)
    assert metrics["brier_score"] == pytest.approx(
        (0.89**2 + 0.12**2 + 0.13**2 + 0.05**2) / 4, abs=1e-6
    )
    item = metrics["reliability_bins"][1]
    assert item["count"] == 3
    assert item["mean_confidence"] == 0.12
    assert item["verified_success_rate"] == 0.333333
    assert item["calibration_gap"] == 0.213333


def test_every_bin_boundary_and_neighbors_are_assigned_once(repo: Path) -> None:
    _task(repo, 0.0)
    _task(repo, 1.0)
    for index in range(1, 10):
        boundary = index / 10
        _task(repo, math.nextafter(boundary, 0))
        _task(repo, boundary)
        _task(repo, math.nextafter(boundary, 1))
    bins = _bins(repo)
    assert [item["count"] for item in bins] == [2, *([3] * 9)]
    assert sum(item["count"] for item in bins) == 29
    assert bins[0]["lower_bound"] == 0
    assert bins[-1]["upper_bound"] == 1


def test_each_exact_internal_boundary_enters_higher_bin(repo: Path) -> None:
    for index in range(11):
        _task(repo, index / 10)
    assert [item["count"] for item in _bins(repo)] == [*([1] * 9), 2]


@pytest.mark.parametrize("success", [False, True])
def test_wilson_intervals_and_all_outcome_boundaries(repo: Path, success: bool) -> None:
    for _ in range(20):
        _task(repo, 0.8, success)
    metrics = _report(repo)["overall"]
    expected = [0.838875, 1.0] if success else [0.0, 0.161125]
    assert metrics["verified_success_interval_95"] == expected
    assert metrics["reliability_bins"][8]["wilson_interval_95"] == expected
    assert _wilson_interval(120, 126)[0] < 0.9


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "missing-confidence"),
        (True, "nonnumeric-confidence"),
        (False, "nonnumeric-confidence"),
        ("0.8", "nonnumeric-confidence"),
        ("bad", "nonnumeric-confidence"),
        ([], "nonnumeric-confidence"),
        (float("nan"), "nonfinite-confidence"),
        (float("inf"), "nonfinite-confidence"),
        (-float("inf"), "nonfinite-confidence"),
        (-0.01, "out-of-range-confidence"),
        (1.01, "out-of-range-confidence"),
        (10**500, "out-of-range-confidence"),
    ],
)
def test_validation_rejects_invalid_raw_values_without_clamping(value: Any, reason: str) -> None:
    assert _confidence_value(value) == (None, reason)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "missing-confidence"),
        (-0.1, "out-of-range-confidence"),
        (1.1, "out-of-range-confidence"),
        (float("inf"), "nonfinite-confidence"),
        (-float("inf"), "nonfinite-confidence"),
        ("NaN", "nonnumeric-confidence"),
        ("true", "nonnumeric-confidence"),
        (SECRET, "nonnumeric-confidence"),
        (b"false", "nonnumeric-confidence"),
    ],
)
def test_invalid_persisted_confidence_is_counted_and_not_scored(
    repo: Path,
    value: Any,
    reason: str,
) -> None:
    _task(repo, value, False)
    report = _report(repo)
    metrics = report["overall"]
    assert metrics["verified_failures"] == 1
    assert metrics["scored_observations"] == 0
    assert metrics["brier_score"] is metrics["expected_calibration_error"] is None
    assert metrics["exclusion_reasons"][reason] == 1
    assert metrics["excluded_tasks"] == 1
    assert metrics["confidence_coverage"] == 0.0
    assert SECRET not in json.dumps(report)


def test_sqlite_type_erasure_is_explicit_and_not_guessed(repo: Path) -> None:
    _task(repo, True)
    _task(repo, False)
    _task(repo, float("nan"))
    metrics = _report(repo)["overall"]
    assert metrics["valid_confidence_tasks"] == 2  # Stored as numeric 1.0 and 0.0.
    assert metrics["missing_confidence_tasks"] == 1  # SQLite stores NaN as NULL.
    assert any("SQLite REAL affinity" in item for item in _report(repo)["limitations"])


def test_boolean_raw_rows_are_rejected_before_model_coercion(repo: Path) -> None:
    _task(repo)
    raw = _ledger(repo).confidence_records(_ledger(repo).repository_id(repo))[0]
    raw["routing_confidence"] = True
    observation = confidence._observation(raw)
    assert observation.confidence is None
    assert observation.confidence_status == "nonnumeric-confidence"
    assert confidence._metrics([observation])["scored_observations"] == 0


def test_coverage_uses_explicit_denominators_and_exclusions_can_overlap(repo: Path) -> None:
    _task(repo, 0.8, True)
    _task(repo, 0.8, False)
    _task(repo, None, True)
    _task(repo, None, None)
    _task(repo, 0.5, None, status="pending")
    metrics = _report(repo)["overall"]
    assert metrics["total_tasks"] == 5
    assert metrics["verified_tasks"] == 3
    assert metrics["unverified_tasks"] == metrics["incomplete_tasks"] == 1
    assert metrics["valid_confidence_tasks"] == 3
    assert metrics["verification_coverage"] == metrics["confidence_coverage"] == 0.6
    assert metrics["scoring_coverage"] == 0.4
    assert metrics["excluded_tasks"] == 3
    assert sum(metrics["exclusion_reasons"].values()) == 4
    assert metrics["verified_success_rate"] == 0.666667
    assert metrics["brier_score"] == 0.34


@pytest.mark.parametrize(
    ("status", "tests", "static", "accepted", "rework", "safety", "expected"),
    [
        ("completed", None, None, None, None, None, "unverified"),
        ("completed", True, None, None, None, None, "verified-success"),
        ("succeeded", None, True, None, None, None, "verified-success"),
        ("completed", None, None, True, None, None, "verified-success"),
        ("completed", None, None, None, False, None, "verified-success"),
        ("completed", False, True, True, False, False, "verified-failure"),
        ("completed", True, False, True, False, False, "verified-failure"),
        ("completed", True, True, False, False, False, "verified-failure"),
        ("completed", True, True, True, True, False, "verified-failure"),
        ("failed", False, None, None, None, False, "incomplete"),
        ("pending", None, None, None, None, True, "verified-failure"),
    ],
)
def test_established_outcome_contract_without_ter(
    repo: Path,
    status: str,
    tests: bool | None,
    static: bool | None,
    accepted: bool | None,
    rework: bool | None,
    safety: bool | None,
    expected: str,
) -> None:
    task_id = _task(repo, outcome=None, status=status)
    ledger = _ledger(repo)
    ledger.record_validation(task_id, tests_passed=tests, static_checks_passed=static)
    ledger.record_human_feedback(task_id, accepted=accepted, rework_required=rework)
    ledger.record_execution(task_id, status=status, safety_violation=safety)
    observation = confidence._load_observations(ledger, repo)[0]
    assert observation.outcome_status == expected
    assert _report(repo)["overall"]["scored_observations"] == int(expected.startswith("verified-"))
    assert ledger.all_execution_attempts() == []


def test_attempts_and_sessions_do_not_multiply_task_observations(repo: Path) -> None:
    task_id = _task(repo, 0.8, False)
    ledger = _ledger(repo)
    for index in range(4):
        attempt = ledger.create_execution_attempt(task_id)
        ledger.link_agent_session(
            attempt.attempt_id,
            agent="claude-code",
            external_session_id=f"session-{index}",
        )
    metrics = _report(repo)["overall"]
    assert metrics["total_tasks"] == metrics["scored_observations"] == 1
    assert metrics["brier_score"] == 0.64


def test_groups_recompute_their_own_scores_and_keep_provenance_separate(repo: Path) -> None:
    _task(repo, 0.9, True, route="local_code", model="z-model", source="model")
    _task(
        repo,
        0.1,
        False,
        route="local_fast",
        model="a-model",
        source="deterministic",
        tier=CapabilityTier.BASIC,
    )
    _task(
        repo,
        0.9,
        False,
        route="frontier_deep",
        model="z-model",
        source="safety-override",
        tier=CapabilityTier.FRONTIER,
    )
    _task(repo, 0.5, True, route="local_code", model="z-model", source="scoreboard")
    report = _report(repo)
    groups = report["groups"]
    assert list(groups["by_route"]) == ["frontier_deep", "local_code", "local_fast"]
    assert list(groups["by_selected_model"]) == ["a-model", "z-model"]
    assert list(groups["by_capability_tier"]) == ["basic", "frontier", "standard"]
    assert set(groups["by_decision_source"]) == {
        "model",
        "deterministic",
        "scoreboard",
        "safety-override",
    }
    assert groups["by_route"]["local_code"]["brier_score"] == 0.13
    assert groups["by_route"]["local_code"]["expected_calibration_error"] == 0.3
    assert groups["by_selected_model"]["z-model"]["brier_score"] == 0.356667
    assert groups["by_capability_tier"]["basic"]["expected_calibration_error"] == 0.1
    assert report["overall"]["provenance_counts"] == {"attributable": 4}
    assert "pooled" in report["population"]
    for dimension in groups.values():
        assert sum(item["total_tasks"] for item in dimension.values()) == 4
        for metrics in dimension.values():
            assert (
                sum(item["count"] for item in metrics["reliability_bins"])
                == metrics["scored_observations"]
            )


@pytest.mark.parametrize(
    ("route", "source", "route_label", "source_label", "provenance"),
    [
        (None, "model", "unavailable", "model", "missing-provenance"),
        ("", None, "unavailable", "unavailable", "missing-provenance"),
        (SECRET, "model", "invalid-route", "model", "invalid-provenance"),
        (["local_fast"], {}, "invalid-route", "invalid-decision-source", "invalid-provenance"),
        ("local_code", SECRET, "local_code", "invalid-decision-source", "invalid-provenance"),
    ],
)
def test_missing_and_invalid_provenance_is_never_inferred(
    repo: Path,
    route: Any,
    source: Any,
    route_label: str,
    source_label: str,
    provenance: str,
) -> None:
    _task(repo, route=route, source=source, model=None, tier=None)
    report = _report(repo)
    groups = report["groups"]
    assert list(groups["by_route"]) == [route_label]
    assert list(groups["by_decision_source"]) == [source_label]
    assert list(groups["by_provenance_status"]) == [provenance]
    assert list(groups["by_capability_tier"]) == ["unavailable"]
    assert list(groups["by_selected_model"]) == ["unavailable"]
    assert report["overall"]["scored_observations"] == 1
    assert SECRET not in json.dumps(report)


def test_nonobject_metadata_is_invalid_provenance_not_inferred(repo: Path) -> None:
    _task(repo, metadata=[SECRET])
    assert _report(repo)["overall"]["provenance_counts"] == {"invalid-provenance": 1}


@pytest.mark.parametrize(
    ("count", "status"), [(0, "missing"), (1, "limited"), (19, "limited"), (20, "sufficient")]
)
def test_descriptive_sufficiency_uses_verified_tasks_with_valid_confidence(
    repo: Path,
    count: int,
    status: str,
) -> None:
    for _ in range(count):
        _task(repo)
    _task(repo, None, True)
    _task(repo, 0.8, None)
    metrics = _report(repo)["overall"]
    assert metrics["evidence_sufficiency"]["status"] == status
    assert metrics["evidence_sufficiency"]["sample_size"] == count
    assert (metrics["brier_score"] is not None) == (count > 0)


def test_report_and_cli_json_are_wholly_deterministic(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for value in [0.3, 0.1, 0.8]:
        _task(repo, value)
    first = json.dumps(_report(repo), allow_nan=False)
    second = json.dumps(_report(repo), allow_nan=False)
    assert first == second
    main(["confidence", "--repo", str(repo), "--json"])
    first_cli = capsys.readouterr().out
    main(["confidence", "--repo", str(repo), "--json"])
    assert first_cli == capsys.readouterr().out
    assert json.loads(first_cli) == json.loads(first)
    assert "timestamp" not in first
    assert "task_id" not in first


def test_cli_text_and_default_repo(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _task(repo)
    monkeypatch.chdir(repo)
    main(["confidence"])
    text = capsys.readouterr().out
    assert "exploratory" in text
    assert "Brier=0.04 ECE=0.2" in text
    assert "[0.9, 1.0]" in text
    assert "Wilson-95%=" in text
    assert "by_decision_source:" in text
    assert "no routing, ranking or threshold changes" in text
    assert SECRET not in text


def test_empty_text_reports_unavailable_metrics(repo: Path) -> None:
    text = render_confidence_report(_report(repo))
    assert "Brier=unavailable ECE=unavailable" in text
    assert "evidence=missing" in text


@pytest.mark.parametrize("flag", ["--apply", "--sessions", "--step-size"])
def test_cli_has_no_mutating_or_legacy_tuning_flags(flag: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["confidence", flag])
    assert exc_info.value.code == 2


@pytest.mark.parametrize("absolute", [False, True])
def test_cli_honors_configured_ledger_path(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    absolute: bool,
) -> None:
    _task(repo, 0.8, False)
    default = _ledger(repo).path
    custom = repo / "custom.sqlite3"
    with closing(sqlite3.connect(default)) as source, closing(sqlite3.connect(custom)) as target:
        source.backup(target)
    _task(repo, 0.9, True)
    config = default_config()
    config.intelligence.outcome_database_path = custom if absolute else Path("custom.sqlite3")
    config_path = tmp_path / "tgv.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    main(["confidence", "--repo", str(repo), "--config", str(config_path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["overall"]["total_tasks"] == 1
    assert report["overall"]["verified_failures"] == 1
    assert report["overall"]["brier_score"] == 0.64


def test_repository_filter_excludes_other_repositories(repo: Path, tmp_path: Path) -> None:
    _task(repo)
    ledger = _ledger(repo)
    profile = TaskProfiler(default_config().intelligence).profile("Fix helper")
    ledger.create_pending(SECRET, tmp_path / "other", profile)
    assert _report(repo)["overall"]["total_tasks"] == 1


def test_read_only_privacy_and_no_routing_ter_or_configuration_changes(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_id = _task(repo, 0.8, True)
    ledger = _ledger(repo)
    attempt = ledger.create_execution_attempt(task_id)
    ledger.link_agent_session(
        attempt.attempt_id,
        agent="claude-code",
        external_session_id="private-session",
        session_path=str(repo / "secret-session.jsonl"),
    )
    config = default_config()
    config.telemetry.enabled = True
    config_path = repo / "tgv.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    with closing(sqlite3.connect(ledger.path)) as connection, connection:
        # Malformed irrelevant data must not be loaded by the confidence command.
        connection.execute(
            "UPDATE outcomes SET routing_reasons = ?, retrieved_files = ?", (SECRET, SECRET)
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
    ledger.path.chmod(0o640)
    before = {
        p.relative_to(repo): (p.read_bytes(), p.stat().st_mode)
        for p in repo.rglob("*")
        if p.is_file()
    }

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("confidence command attempted a mutating or execution operation")

    monkeypatch.setattr(TGVService, "__init__", forbidden)
    monkeypatch.setattr(Router, "route", forbidden)
    monkeypatch.setattr("tgv.scoreboard.RoutingScoreboard.recommend", forbidden)
    monkeypatch.setattr("tgv.efficiency.ter_analyzer", forbidden)
    monkeypatch.setattr("tgv.linked_efficiency.measure_linked_sessions", forbidden)
    monkeypatch.setattr(OutcomeLedger, "_initialize", forbidden)
    monkeypatch.setattr(OutcomeLedger, "_connect", forbidden)
    main(["confidence", "--repo", str(repo), "--config", str(config_path), "--json"])
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["overall"]["scored_observations"] == 1
    assert SECRET not in output
    assert "secret-session.jsonl" not in output
    assert str(repo) not in output
    assert task_id not in output
    after = {
        p.relative_to(repo): (p.read_bytes(), p.stat().st_mode)
        for p in repo.rglob("*")
        if p.is_file()
    }
    assert after == before


@pytest.mark.parametrize("version", [1, 2, "invalid-version"])
def test_legacy_or_invalid_schema_is_rejected_without_migration(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    version: Any,
) -> None:
    _task(repo)
    database = _ledger(repo).path
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE schema_version SET version = ?", (version,))
    before = database.read_bytes()
    with pytest.raises(SystemExit) as exc_info:
        main(["confidence", "--repo", str(repo), "--json"])
    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "schema version" in error
    assert database.read_bytes() == before


def test_missing_schema_tables_are_actionable(repo: Path) -> None:
    database = _ledger(repo).path
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER)")
    with pytest.raises(ConfidenceDiagnosticError, match="missing required tables"):
        _report(repo)


def test_malformed_database_is_actionable_without_content_leak(repo: Path) -> None:
    database = _ledger(repo).path
    database.parent.mkdir()
    database.write_text(SECRET)
    with pytest.raises(ConfidenceDiagnosticError) as exc_info:
        _report(repo)
    assert "Cannot read confidence evidence" in str(exc_info.value)
    assert SECRET not in str(exc_info.value)
    assert database.read_text() == SECRET


def test_database_path_that_is_directory_is_not_treated_as_missing(repo: Path) -> None:
    _ledger(repo).path.mkdir(parents=True)
    with pytest.raises(ConfidenceDiagnosticError, match="Cannot read confidence evidence"):
        _report(repo)


def test_missing_projection_column_produces_safe_schema_error(repo: Path) -> None:
    _task(repo)
    with closing(sqlite3.connect(_ledger(repo).path)) as connection, connection:
        connection.execute("ALTER TABLE outcomes DROP COLUMN routing_confidence")
    with pytest.raises(ConfidenceDiagnosticError, match="malformed outcome schema"):
        _report(repo)


@pytest.mark.parametrize("column", ["task_category", "timestamp", "selected_capability_tier"])
def test_malformed_task_errors_do_not_echo_raw_values(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    column: str,
) -> None:
    _task(repo)
    with closing(sqlite3.connect(_ledger(repo).path)) as connection, connection:
        # Column comes only from the fixed parametrization above.
        connection.execute(f"UPDATE outcomes SET {column} = ?", (SECRET,))
    with pytest.raises(SystemExit) as exc_info:
        main(["confidence", "--repo", str(repo), "--json"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "malformed task fields or routing metadata" in captured.err
    assert SECRET not in captured.err + captured.out


def test_malformed_metadata_is_attributed_without_echoing_raw_values(repo: Path) -> None:
    _task(repo)
    with closing(sqlite3.connect(_ledger(repo).path)) as connection, connection:
        connection.execute("UPDATE outcomes SET metadata = ?", (SECRET,))
    report = _report(repo)
    assert report["overall"]["scored_observations"] == 1
    assert report["groups"]["by_provenance_status"]["invalid-provenance"]["total_tasks"] == 1
    assert report["groups"]["by_decision_source"]["invalid-decision-source"]["total_tasks"] == 1
    assert SECRET not in json.dumps(report)


def test_duplicate_task_ids_in_malformed_table_are_rejected(repo: Path) -> None:
    _task(repo)
    with closing(sqlite3.connect(_ledger(repo).path)) as connection, connection:
        connection.execute("CREATE TABLE copied AS SELECT * FROM outcomes")
        connection.execute("DROP TABLE outcomes")
        connection.execute("ALTER TABLE copied RENAME TO outcomes")
        connection.execute("INSERT INTO outcomes SELECT * FROM outcomes")
    with pytest.raises(ConfidenceDiagnosticError, match="duplicate outcome task ids"):
        _report(repo)


def test_model_labels_cannot_inject_terminal_controls(repo: Path) -> None:
    _task(repo, model="name\n\x1b[31m")
    text = render_confidence_report(_report(repo))
    assert "\x1b" not in text
    assert "name\\n\\u001b[31m" in text


def test_existing_override_confidence_semantics_are_preserved(repo: Path) -> None:
    baseline = RouteDecision(route=Route.LOCAL_CODE, confidence=0.72, rationale="baseline")
    profile = TaskProfiler(default_config().intelligence).profile("Fix helper")
    recommendation = ScoreboardRecommendation(
        recommended_model="other-model",
        recommended_capability_tier=CapabilityTier.FRONTIER,
        confidence=0.99,
        expected_success_probability=0.95,
        explanation="test",
    )
    scoreboard = TGVService._apply_recommendation(baseline, profile, recommendation)
    safety = TGVService._apply_recommendation(
        baseline.model_copy(update={"risk_signals": ["risk"]}),
        profile,
        recommendation,
    )
    assert scoreboard.decision_source == "scoreboard"
    assert safety.decision_source == "safety-override"
    for decision in (scoreboard, safety):
        assert decision.route == Route.FRONTIER_DEEP
        assert decision.confidence == baseline.confidence
        _task(
            repo, decision.confidence, source=decision.decision_source, route=decision.route.value
        )
    report = _report(repo)
    assert report["overall"]["reliability_bins"][7]["mean_confidence"] == 0.72
    assert any("retain baseline routing confidence" in text for text in report["limitations"])


def test_existing_legacy_commands_still_dispatch(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("tgv.cli.score_sessions", lambda path: [])
    main(["calibrate", "--sessions", str(repo), "--format", "json"])
    assert len(json.loads(capsys.readouterr().out)) == 2
    main(["calibrate", "--evidence", "--repo", str(repo), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert all(not proposal["application_permitted"] for proposal in report["proposals"])
    main(["benchmark", "observed", "--repo", str(repo), "--json"])
    assert json.loads(capsys.readouterr().out)["source"] == "observed-ledger"
    main(["efficiency", "--sessions", str(repo), "--format", "json"])
    assert json.loads(capsys.readouterr().out) == {}
