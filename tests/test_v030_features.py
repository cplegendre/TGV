from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tgv.config import default_config, load_config
from tgv.exceptions import ConfigurationError
from tgv.models import CapabilityTier, Route
from tgv.providers import CommandHandoffProvider
from tgv.repository import Repository
from tgv.service import TGVService


def _repo(tmp_path: Path, *, persist_index: bool = True) -> Repository:
    (tmp_path / "app.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    config = default_config()
    return Repository(
        tmp_path,
        config.repository,
        config.privacy.deny_patterns,
        persist_index=persist_index,
    )


def _seed_successes(service: TGVService, root: Path, model: str, count: int = 5) -> None:
    for index in range(count):
        profile = service.profiler.profile("fix helper bug")
        task_id = service.outcomes.create_pending(f"fix helper bug {model} {index}", root, profile)
        service.outcomes.record_routing(
            task_id,
            revised_profile=profile,
            confidence=0.9,
            model=model,
            capability_tier=CapabilityTier.STANDARD,
            reasons=["test seed"],
            files=["app.py"],
            symbols=["helper"],
            context_token_estimate=8,
        )
        service.outcomes.record_execution(task_id, status="completed", latency_seconds=0.1)
        service.outcomes.record_validation(
            task_id, tests_passed=True, static_checks_passed=True
        )
        service.outcomes.record_human_feedback(
            task_id, accepted=True, rework_required=False
        )


def test_config_rejects_unknown_keys_and_invalid_regex(tmp_path: Path) -> None:
    typo = tmp_path / "typo.yaml"
    typo.write_text(
        "version: 1\nmodels: {}\nrouting:\n  locla_answer_confidence: 0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="locla_answer_confidence"):
        load_config(typo)

    regex = tmp_path / "regex.yaml"
    regex.write_text(
        'version: 1\nmodels: {}\nrouting:\n  patterns:\n    simple: "["\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="invalid regular expression"):
        load_config(regex)

    high_risk = tmp_path / "high-risk.yaml"
    high_risk.write_text(
        'version: 1\nmodels: {}\nrouting:\n  patterns:\n    high_risk:\n      bad: "("\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="high_risk.bad"):
        load_config(high_risk)


def test_intelligence_flag_disables_adaptive_state(tmp_path: Path) -> None:
    config = default_config()
    config.routing.use_local_model = False
    config.intelligence.enabled = False
    service = TGVService(config, _repo(tmp_path))
    decision = service.route("fix helper bug")
    assert decision.task_profile is None
    assert decision.scoreboard_recommendation is None
    assert decision.selected_model == config.models["code"].model
    assert decision.selected_capability_tier == CapabilityTier.STANDARD
    assert not (tmp_path / ".tgv/outcomes.sqlite3").exists()
    service.close()


def test_read_only_service_has_no_repository_side_effects(tmp_path: Path) -> None:
    config = default_config()
    config.routing.use_local_model = False
    config.telemetry.enabled = True
    service = TGVService(config, _repo(tmp_path, persist_index=False), read_only=True)
    decision = service.route("fix helper bug")
    bundle = service.prepare("fix helper bug")
    assert decision.task_profile is not None
    assert decision.scoreboard_recommendation is None
    assert "Read-only mode" in decision.rationale
    assert bundle.evidence
    assert not (tmp_path / ".tgv").exists()
    service.close()


def test_adaptive_model_is_applied_and_repository_scoped(tmp_path: Path) -> None:
    config = default_config()
    config.routing.use_local_model = False
    service = TGVService(config, _repo(tmp_path))
    _seed_successes(service, tmp_path, "repo-best")

    other = tmp_path / "other-repo"
    other.mkdir()
    _seed_successes(service, other, "other-best", count=8)

    decision = service.route("fix helper bug")
    assert decision.route == Route.LOCAL_CODE
    assert decision.decision_source == "scoreboard"
    assert decision.selected_model == "repo-best"
    assert decision.selected_capability_tier == CapabilityTier.STANDARD

    bundle = service.prepare("fix helper bug")
    assert bundle.decision.selected_model == "repo-best"
    records = service.outcomes.query(
        model="repo-best", repository_id=service.outcomes.repository_id(tmp_path), limit=20
    )
    assert any(record.routing_confidence == bundle.decision.confidence for record in records)
    service.close()


def test_unverified_completed_outcome_is_not_counted_as_success(tmp_path: Path) -> None:
    service = TGVService(default_config(), _repo(tmp_path))
    profile = service.profiler.profile("fix helper bug")
    task_id = service.outcomes.create_pending("fix helper bug", tmp_path, profile)
    service.outcomes.record_routing(
        task_id,
        revised_profile=profile,
        confidence=0.9,
        model="unverified",
        capability_tier=CapabilityTier.STANDARD,
        reasons=[],
        files=[],
        symbols=[],
        context_token_estimate=0,
    )
    service.outcomes.record_execution(task_id, status="completed")
    record = service.outcomes.query(model="unverified", limit=1)[0]
    assert service.scoreboard._success(record) == 0
    assert service.scoreboard._success(record.model_copy(update={"tests_passed": False})) == 0
    assert (
        service.scoreboard._success(record.model_copy(update={"human_accepted": False})) == 0
    )
    service.close()


def test_command_metadata_redacts_secrets_and_bounds_output(tmp_path: Path) -> None:
    dry = CommandHandoffProvider(
        ["agent", "--api-key", "SUPERSECRET", "--token=TOKENVALUE"]
    ).invoke_with_metadata("h", "j", dry_run=True)
    assert "SUPERSECRET" not in " ".join(dry.command)
    assert "TOKENVALUE" not in " ".join(dry.command)
    assert "<redacted>" in " ".join(dry.command)

    code = "import sys; print('x'*10000); print('y'*10000, file=sys.stderr)"
    metadata = CommandHandoffProvider([sys.executable, "-c", code]).invoke_with_metadata(
        str(tmp_path / "h"), str(tmp_path / "j")
    )
    assert len(metadata.stdout_excerpt) == 2000
    assert len(metadata.stderr_excerpt) == 2000


def test_repository_prunes_ignored_directories(tmp_path: Path, monkeypatch) -> None:
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "huge.js").write_text("needle", encoding="utf-8")
    (tmp_path / "safe.py").write_text("needle", encoding="utf-8")
    repo = _repo(tmp_path)
    visited: list[str] = []
    real_walk = __import__("os").walk

    def observing_walk(*args: object, **kwargs: object):
        for directory, dirnames, filenames in real_walk(*args, **kwargs):
            visited.append(str(directory))
            yield directory, dirnames, filenames

    monkeypatch.setattr("tgv.repository.os.walk", observing_walk)
    assert "safe.py" in repo.list_files(100)
    assert not any("node_modules" in directory for directory in visited)


def test_execution_plan_helpers_and_disabled_outcome_guard(tmp_path: Path) -> None:
    config = default_config()
    config.routing.use_local_model = False
    service = TGVService(config, _repo(tmp_path))
    assert service._tier_for_route(Route.FRONTIER_STANDARD) == CapabilityTier.ADVANCED
    assert service._tier_for_route(Route.FRONTIER_DEEP) == CapabilityTier.FRONTIER
    config.intelligence.enabled = False
    with pytest.raises(Exception, match="Outcome persistence is disabled"):
        service.create_outcome("fix helper bug")
    service.close()


def test_safety_override_discards_adaptive_local_model(tmp_path: Path) -> None:
    from tgv.models import ScoreboardRecommendation

    config = default_config()
    config.routing.use_local_model = False
    service = TGVService(config, _repo(tmp_path))
    baseline = service.router.deterministic("delete production database")
    profile = service.profiler.profile("delete production database")
    recommendation = ScoreboardRecommendation(
        recommended_model="unsafe-local",
        recommended_capability_tier=CapabilityTier.STANDARD,
        explanation="historical candidate",
    )
    applied = service._apply_recommendation(baseline, profile, recommendation)
    planned = service._with_execution_plan(applied)
    assert planned.route == Route.FRONTIER_DEEP
    assert planned.decision_source == "safety-override"
    assert planned.selected_model == config.models["frontier_deep"].model
    assert planned.selected_capability_tier == CapabilityTier.FRONTIER
    service.close()
