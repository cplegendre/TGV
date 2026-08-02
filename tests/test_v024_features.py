import json

from tgv.cli import main
from tgv.efficiency import ScoredSession, value_report
from tgv.models import CapabilityTier, IntelligenceConfig, TaskCategory, TaskProfile
from tgv.outcomes import OutcomeLedger
from tgv.scoreboard import RoutingScoreboard


def profile() -> TaskProfile:
    return TaskProfile(
        task_category=TaskCategory.BUG_FIX,
        complexity_score=30,
        confidence=0.8,
        scope_score=20,
        ambiguity_score=20,
        architectural_impact_score=10,
        verification_difficulty_score=30,
        security_risk_score=0,
        context_uncertainty_score=10,
        recommended_capability_tier=CapabilityTier.STANDARD,
    )


def test_scoreboard_cold_start_has_reason(tmp_path) -> None:
    scoreboard = RoutingScoreboard(
        OutcomeLedger(tmp_path / "outcomes.sqlite3"), IntelligenceConfig()
    )
    result = scoreboard.recommend(profile())
    assert result.fallback_used is True
    assert "scoreboard_minimum_samples" in (result.fallback_reason or "")


def test_outcome_complete_and_report_cli(tmp_path, sample_repo, monkeypatch, capsys) -> None:
    ledger = OutcomeLedger(sample_repo / ".tgv/outcomes.sqlite3")
    task_id = ledger.create_pending("fix", sample_repo, profile())
    main(["outcomes", "complete", task_id, "--accepted", "--repo", str(sample_repo)])
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    record = ledger.query(limit=1)[0]
    assert record.execution_status == "completed"
    assert record.human_accepted is True

    sessions = [ScoredSession("local_fast", 0.9, 20, 5)]
    monkeypatch.setattr("tgv.cli.score_sessions", lambda _path: sessions)
    main(["report", "--sessions", str(tmp_path), "--ter", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["local_share"] >= 0
    assert report["savings_tokens"] >= 0
    assert report["savings_ratio"] >= 0
    assert value_report([])["local_share"] == 0


def test_report_cli_works_without_ter_by_default(tmp_path, capsys) -> None:
    """`tgv report` must be runnable with zero extra dependencies installed."""
    from tgv.models import Route, RouteDecision
    from tgv.telemetry import SessionRecorder

    sessions_dir = tmp_path / "sessions"
    decision = RouteDecision(
        route=Route.LOCAL_FAST,
        confidence=0.8,
        rationale="test",
        risk_signals=[],
        search_terms=[],
        suggested_validation=[],
        matched_category="test",
        matched_pattern=None,
        decision_source="deterministic",
        matched_signals=[],
    )
    SessionRecorder(sessions_dir).record("a request", decision)

    main(["report", "--sessions", str(sessions_dir), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["sessions"] == 1
    assert report["local_share"] == 1.0


def test_basic_value_report_handles_missing_directory(tmp_path) -> None:
    from tgv.efficiency import basic_value_report

    report = basic_value_report(tmp_path / "does-not-exist")
    assert report == {
        "sessions": 0,
        "local_share": 0.0,
        "savings_tokens": 0,
        "savings_ratio": 0.0,
    }


def test_basic_value_report_ignores_file_without_route_and_mixes_frontier(tmp_path) -> None:
    from tgv.efficiency import basic_value_report

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "no_route.jsonl").write_text(
        '{"type": "user", "message": {"content": []}}\n\n', encoding="utf-8"
    )
    (sessions_dir / "frontier.jsonl").write_text(
        '{"route_decision": {"route": "frontier_deep"}, '
        '"message": {"usage": {"input_tokens": 10, "output_tokens": 10}}}\n',
        encoding="utf-8",
    )

    report = basic_value_report(sessions_dir)
    assert report["sessions"] == 1
    assert report["local_share"] == 0.0
    assert report["savings_tokens"] == 0
