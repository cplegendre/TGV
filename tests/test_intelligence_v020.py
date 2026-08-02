from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgv.config import default_config
from tgv.evaluation import run_benchmark
from tgv.intelligence import CodeKnowledgeBase, TaskProfiler
from tgv.models import CapabilityTier, Route, TaskCategory
from tgv.outcomes import OutcomeLedger
from tgv.repository import Repository
from tgv.scoreboard import RoutingScoreboard
from tgv.service import TGVService


def make_repo(tmp_path: Path) -> Repository:
    (tmp_path / "app.py").write_text(
        "import asyncio\n\nclass Service:\n    def run(self):\n        return helper()\n\n"
        "def helper():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import helper\n\ndef test_helper(): assert helper() == 1\n", encoding="utf-8"
    )
    config = default_config()
    return Repository(tmp_path, config.repository, config.privacy.deny_patterns)


def test_profiler_categories_tiers_and_revised_evidence(tmp_path: Path) -> None:
    config = default_config()
    profiler = TaskProfiler(config.intelligence)
    assert profiler.profile("Update README docs").task_category == TaskCategory.DOCUMENTATION
    assert profiler.profile("format whitespace style").task_category == TaskCategory.FORMATTING
    assert profiler.profile("change one label").task_category == TaskCategory.SMALL_EDIT
    assert (
        profiler.profile("refactor all modules across the codebase").task_category
        == TaskCategory.REFACTOR
    )
    assert profiler.profile("fix regression exception").task_category == TaskCategory.BUG_FIX
    assert profiler.profile("implement a feature").task_category == TaskCategory.FEATURE
    assert profiler.profile("add pytest coverage").task_category == TaskCategory.TESTING
    assert (
        profiler.profile("redesign architecture migration").task_category
        == TaskCategory.ARCHITECTURE
    )
    security = profiler.profile("rotate production credentials for authentication")
    assert security.task_category == TaskCategory.SECURITY
    assert security.recommended_capability_tier == CapabilityTier.FRONTIER
    assert security.risk_factors
    assert profiler.profile("optimize latency benchmark").task_category == TaskCategory.PERFORMANCE
    assert (
        profiler.profile("upgrade dependency in pyproject").task_category
        == TaskCategory.DEPENDENCY_CHANGE
    )
    assert (
        profiler.profile("where is repository config").task_category
        == TaskCategory.REPOSITORY_INVESTIGATION
    )
    unknown = profiler.profile("hello")
    assert unknown.task_category == TaskCategory.UNKNOWN
    assert unknown.recommended_capability_tier == CapabilityTier.FRONTIER

    repo = make_repo(tmp_path)
    evidence = repo.search("Service helper")
    revised = profiler.profile("maybe refactor all modules", evidence, stage="revised")
    assert revised.stage == "revised"
    assert revised.evidence_paths
    assert revised.evidence_symbols
    assert revised.relevant_signals["relevant_files"] >= 1
    assert 0 <= revised.ambiguity_score <= 100


def test_code_knowledge_base_budget_and_scores(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    config = default_config()
    kb = CodeKnowledgeBase(repo, config.intelligence)
    results = kb.retrieve("Service helper")
    assert results
    assert results[0].lexical_score > 0
    assert results[0].final_score > 0
    assert results[0].estimated_token_cost > 0
    assert results[0].semantic_score == 0
    assert results == sorted(
        results, key=lambda item: (-item.final_score, item.path, item.line_range[0])
    )

    tiny = config.intelligence.model_copy(update={"context_token_budget": 256})
    assert CodeKnowledgeBase(repo, tiny).retrieve("Service helper")
    too_tiny = config.intelligence.model_copy(update={"context_token_budget": 1})
    assert CodeKnowledgeBase(repo, too_tiny).retrieve("Service helper") == []


def test_outcome_ledger_full_lifecycle_and_exports(tmp_path: Path) -> None:
    db = tmp_path / "outcomes.sqlite3"
    ledger = OutcomeLedger(db)
    profile = TaskProfiler(default_config().intelligence).profile("fix bug")
    task_id = ledger.create_pending("fix bug", tmp_path, profile, metadata={"source": "test"})
    ledger.record_routing(
        task_id,
        revised_profile=profile.model_copy(update={"stage": "revised"}),
        confidence=0.8,
        model="local-code",
        capability_tier=CapabilityTier.STANDARD,
        reasons=["bounded code"],
        files=["app.py"],
        symbols=["helper"],
        context_token_estimate=42,
    )
    ledger.record_execution(
        task_id,
        status="completed",
        latency_seconds=1.2,
        input_tokens=10,
        output_tokens=20,
        estimated_cost=0.01,
    )
    ledger.record_validation(task_id, tests_passed=True, static_checks_passed=True)
    ledger.record_human_feedback(task_id, accepted=True, rework_required=False)
    ledger.record_retry(task_id)
    ledger.record_retry(task_id, escalated=True)
    records = ledger.query(
        category=TaskCategory.BUG_FIX,
        model="local-code",
        repository_id=ledger.repository_id(tmp_path),
        since=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert len(records) == 1
    record = records[0]
    assert record.retry_count == 2
    assert record.execution_status == "escalated"
    assert record.tests_passed is True
    assert "request_hash" in ledger.export_json(records)
    assert "task_id" in ledger.export_csv(records)
    assert ledger.export_csv([]) == ""
    assert ledger.query(category=TaskCategory.SECURITY) == []
    with pytest.raises(KeyError):
        ledger.record_execution("missing", status="failed")
    ledger._update(task_id)


def seed_outcomes(
    ledger: OutcomeLedger, tmp_path: Path, *, model: str, successes: int, failures: int
) -> None:
    profiler = TaskProfiler(default_config().intelligence)
    for index in range(successes + failures):
        profile = profiler.profile("fix bug")
        task_id = ledger.create_pending(f"fix bug {model} {index}", tmp_path, profile)
        ledger.record_routing(
            task_id,
            revised_profile=profile,
            confidence=0.8,
            model=model,
            capability_tier=CapabilityTier.STANDARD,
            reasons=[],
            files=[],
            symbols=[],
            context_token_estimate=0,
        )
        ok = index < successes
        ledger.record_execution(
            task_id,
            status="completed" if ok else "failed",
            latency_seconds=1.0,
            estimated_cost=0.02,
        )
        ledger.record_validation(task_id, tests_passed=ok, static_checks_passed=ok)
        ledger.record_human_feedback(task_id, accepted=ok, rework_required=not ok)


def test_scoreboard_small_sample_and_ranked_recommendation(tmp_path: Path) -> None:
    config = default_config()
    ledger = OutcomeLedger(tmp_path / "scores.sqlite3")
    profile = TaskProfiler(config.intelligence).profile("fix bug")
    scoreboard = RoutingScoreboard(ledger, config.intelligence)
    fallback = scoreboard.recommend(profile)
    assert fallback.fallback_used
    seed_outcomes(ledger, tmp_path, model="good", successes=5, failures=0)
    seed_outcomes(ledger, tmp_path, model="bad", successes=1, failures=4)
    recommendation = scoreboard.recommend(profile)
    assert recommendation.recommended_model == "good"
    assert recommendation.sample_size == 5
    assert recommendation.alternative_candidates
    assert recommendation.expected_success_probability is not None
    assert recommendation.utility_score is not None


def test_service_integrates_profile_scoreboard_and_safety(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    service = TGVService(default_config(), repo)
    profile = service.profile("fix helper bug")
    assert profile.stage == "revised"
    assert service.profile("fix helper bug", retrieve=False).stage == "initial"
    assert service.retrieve("helper")
    decision = service.route("fix helper bug")
    assert decision.task_profile is not None
    assert decision.scoreboard_recommendation is not None
    assert decision.scoreboard_recommendation.fallback_used
    dangerous = service.route("delete production database")
    assert dangerous.route == Route.FRONTIER_DEEP
    assert dangerous.decision_source == "safety-override"
    task_id = service.create_outcome("fix helper bug")
    assert service.outcomes.query(limit=1)[0].task_id == task_id
    service.close()


def test_evaluation_runner(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "case_id": "a",
                        "request": "Update README docs",
                        "expected_task_category": "documentation",
                        "expected_complexity_band": "low",
                        "acceptable_routes": ["frontier_deep", "local_code", "local_fast"],
                        "expected_risk_factors": [],
                    }
                ),
                json.dumps(
                    {
                        "case_id": "b",
                        "request": "rotate production credentials",
                        "expected_task_category": "security",
                        "expected_complexity_band": "high",
                        "acceptable_routes": ["frontier_deep"],
                        "expected_risk_factors": ["security"],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    report = run_benchmark(cases)
    assert report["total_cases"] == 2
    assert report["acceptable_route_accuracy"] == 1.0
    assert "cases=2" in report["summary"]


def test_additional_profile_and_scoreboard_branches(tmp_path: Path) -> None:
    from tgv.models import ScoreboardRecommendation

    profiler = TaskProfiler(default_config().intelligence)
    broad = profiler.profile(
        "maybe possibly unclear refactor all modules across the entire codebase"
    )
    assert "Request scope is ambiguous" in broad.risk_factors
    repo = make_repo(tmp_path)
    evidence = repo.search("helper")
    no_tests = [item for item in evidence if "test" not in item.path]
    revised = profiler.profile("refactor helper", no_tests, stage="revised")
    assert "No directly associated tests were found" in revised.risk_factors
    assert TGVService._route_for_tier(CapabilityTier.BASIC) == Route.LOCAL_FAST
    assert TGVService._route_for_tier(CapabilityTier.ADVANCED) == Route.LOCAL_CODE
    assert TGVService._route_for_tier(None) == Route.FRONTIER_DEEP
    baseline = TGVService(default_config(), repo).router.deterministic("fix helper")
    rec = ScoreboardRecommendation(
        recommended_model="x",
        recommended_capability_tier=CapabilityTier.STANDARD,
        expected_success_probability=0.9,
        expected_cost=0.1,
        expected_latency=1.0,
        utility_score=90,
        confidence=0.8,
        sample_size=10,
        explanation="historical win",
    )
    applied = TGVService._apply_recommendation(baseline, revised, rec)
    assert applied.decision_source == "scoreboard"
    assert applied.route == Route.LOCAL_CODE


def test_scoreboard_safety_and_failure_paths(tmp_path: Path) -> None:
    config = default_config()
    ledger = OutcomeLedger(tmp_path / "safety.sqlite3")
    profile = TaskProfiler(config.intelligence).profile("fix bug")
    task_id = ledger.create_pending("fix bug unsafe", tmp_path, profile)
    ledger.record_routing(
        task_id,
        revised_profile=profile,
        confidence=0.5,
        model="unsafe",
        capability_tier=CapabilityTier.STANDARD,
        reasons=[],
        files=[],
        symbols=[],
        context_token_estimate=0,
    )
    ledger.record_execution(task_id, status="completed", safety_violation=True)
    ledger.record_validation(task_id, tests_passed=True, static_checks_passed=False)
    ledger.record_human_feedback(task_id, accepted=False, rework_required=True)
    board = RoutingScoreboard(
        ledger, config.intelligence.model_copy(update={"scoreboard_minimum_samples": 1})
    )
    rec = board.recommend(profile)
    assert rec.recommended_model == "unsafe"
    assert rec.expected_success_probability is not None and rec.expected_success_probability < 0.5
