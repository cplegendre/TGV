from __future__ import annotations

import io
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

from tgv.cli import main
from tgv.config import default_config
from tgv.daemon import RouteWorker, serve
from tgv.evaluation import run_outcome_benchmark
from tgv.explain import _counterfactual, explain_decision, render_explanation
from tgv.models import (
    CapabilityTier,
    OutcomeTask,
    Route,
    RouteDecision,
    ScoreboardRecommendation,
    TaskCategory,
)
from tgv.observability import (
    atomic_text,
    health_report,
    prometheus_text,
    write_prometheus,
)
from tgv.repository import Repository
from tgv.router import Router
from tgv.service import TGVService


def _service(tmp_path: Path, *, intelligence: bool = False) -> TGVService:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    config = default_config()
    config.routing.use_local_model = False
    config.intelligence.enabled = intelligence
    return TGVService(
        config,
        Repository(tmp_path, config.repository, config.privacy.deny_patterns),
    )


def _outcome(
    index: int,
    *,
    success: bool = True,
    accepted: bool | None = True,
    safety: bool | None = False,
    model: str = "code-model",
    tier: CapabilityTier = CapabilityTier.STANDARD,
) -> OutcomeTask:
    return OutcomeTask(
        task_id=f"task-{index}",
        timestamp=datetime.now(UTC),
        repository_id="repo",
        request_hash=f"hash-{index}",
        task_category=TaskCategory.BUG_FIX if index % 2 else TaskCategory.TESTING,
        selected_model=model,
        selected_capability_tier=tier,
        execution_status="completed",
        tests_passed=success,
        static_checks_passed=success,
        human_accepted=accepted,
        rework_required=False if accepted is not False else True,
        retry_count=1 if index % 5 == 0 else 0,
        latency_seconds=float(index + 1),
        estimated_cost=0.01 * (index + 1),
        safety_violation=safety,
    )


def test_counterfactual_explanation_and_rendering(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        report = service.explain("Fix the parser bug in this file and add a regression test")
        decision = report["decision"]
        assert isinstance(decision, dict)
        assert decision["route"] == "local_code"
        assert report["why_selected"]
        alternatives = report["what_would_change_the_decision"]
        assert isinstance(alternatives, list)
        assert {item["target_route"] for item in alternatives} == {
            "local_fast",
            "frontier_standard",
            "frontier_deep",
        }
        rendered = render_explanation(report)
        assert "What would make another route win" in rendered
        assert "Selected: local_code" in rendered
    finally:
        service.close()


def test_counterfactual_safety_and_scoreboard_paths() -> None:
    config = default_config()
    config.routing.use_local_model = False
    router = Router(config.routing)
    safety = router.deterministic("Fix the authentication vulnerability and rotate credentials")
    safety_report = explain_decision(
        router, "Fix the authentication vulnerability and rotate credentials", safety
    )
    local = next(
        item
        for item in safety_report["what_would_change_the_decision"]
        if item["target_route"] == "local_code"
    )
    assert "high-risk" in local["changes"][0]

    baseline = router.deterministic("Implement a bounded parser change in this module")
    recommendation = ScoreboardRecommendation(
        recommended_model="history-model",
        recommended_capability_tier=CapabilityTier.BASIC,
        expected_success_probability=0.95,
        confidence=0.9,
        sample_size=12,
        explanation="history wins",
    )
    scoreboard_decision = baseline.model_copy(
        update={
            "route": Route.LOCAL_FAST,
            "selected_model": "history-model",
            "selected_capability_tier": CapabilityTier.BASIC,
            "decision_source": "scoreboard",
            "scoreboard_recommendation": recommendation,
        }
    )
    scoreboard = explain_decision(
        router, "Implement a bounded parser change in this module", scoreboard_decision
    )
    assert any("historical outcomes" in reason for reason in scoreboard["why_selected"])
    assert any(
        "historical utility" in change
        for item in scoreboard["what_would_change_the_decision"]
        for change in item["changes"]
    )

    manual = RouteDecision(
        route=Route.FRONTIER_STANDARD,
        confidence=0.5,
        rationale="manual",
        score_breakdown={route.value: 0.0 for route in Route},
    )
    manual_report = explain_decision(router, "opaque request", manual)
    assert manual_report["why_selected"]

    safety_override = manual.model_copy(update={"decision_source": "safety-override"})
    override_report = explain_decision(router, "opaque request", safety_override)
    assert any("safety policy" in reason for reason in override_report["why_selected"])

    higher_target = RouteDecision(
        route=Route.LOCAL_FAST,
        confidence=0.5,
        rationale="model-selected",
        score_breakdown={
            "local_fast": 0.0,
            "local_code": 5.0,
            "frontier_standard": 0.0,
            "frontier_deep": 0.0,
        },
    )
    higher_report = explain_decision(router, "Where is parser?", higher_target)
    assert any(
        "already has a higher deterministic score" in item["note"]
        for item in higher_report["what_would_change_the_decision"]
    )

    empty_weights = Router(config.routing)
    empty_weights.WEIGHTS = {route: {} for route in Route}  # type: ignore[assignment]
    impossible = RouteDecision(
        route=Route.LOCAL_FAST,
        confidence=0.5,
        rationale="manual",
        score_breakdown={
            "local_fast": 100.0,
            "local_code": 0.0,
            "frontier_standard": 0.0,
            "frontier_deep": 0.0,
        },
    )
    no_small_change = _counterfactual(
        empty_weights, "opaque", impossible, Route.FRONTIER_DEEP
    )
    assert "No small request-signal change" in no_small_change.note
    assert no_small_change.changes


def test_prometheus_export_health_draining_and_private_files(tmp_path: Path) -> None:
    status = {
        "state": "ready",
        "ready": True,
        "draining": False,
        "requests_handled": 5,
        "requests_succeeded": 4,
        "requests_failed": 1,
        "requests_timed_out": 1,
        "requests_cancelled": 0,
        "worker_restarts": 2,
        "route_counts": {"local_code": 3, "local_fast": 1},
        "service_metrics": {
            "routing": {
                "total": 5,
                "errors": 1,
                "latency_ms": {"average": 10, "p50": 8, "p95": 20, "max": 25},
            },
            "providers": {
                "calls": {"ollama:qwen:4b": 3},
                "errors": {"ollama:qwen:4b": 1},
            },
        },
    }
    rendered = prometheus_text(status)
    assert 'tgv_requests_total{result="handled"} 5' in rendered
    assert 'tgv_route_requests_total{route="local_code"} 3' in rendered
    assert 'tgv_provider_calls_total{model="qwen:4b",provider="ollama"} 3' in rendered
    assert 'tgv_service_routing_latency_ms{statistic="p95"} 20.0' in rendered

    metrics = tmp_path / "metrics.prom"
    write_prometheus(metrics, status)
    assert metrics.read_text() == rendered
    private = tmp_path / "private.txt"
    atomic_text(private, "secret-ish metadata\n")
    assert private.read_text() == "secret-ish metadata\n"
    if os.name == "posix":
        assert private.stat().st_mode & 0o077 == 0
        assert metrics.stat().st_mode & 0o077 == 0

    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "ready": False,
                "draining": True,
                "state": "draining",
                "heartbeat_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    report = health_report(status_file)
    assert report["healthy"] is False
    assert report["draining"] is True
    assert report["reason"] == "daemon is draining"

    malformed = prometheus_text(
        {
            "ready": False,
            "service_metrics": {
                "routing": {"latency_ms": "unknown"},
                "providers": {"calls": "unknown", "errors": "unknown"},
            },
        }
    )
    assert "tgv_ready 0" in malformed

    original_chmod = Path.chmod
    try:
        Path.chmod = (  # type: ignore[method-assign]
            lambda self, mode: (_ for _ in ()).throw(OSError("unsupported"))
        )
        fallback = tmp_path / "chmod-fallback.txt"
        atomic_text(fallback, "ok")
        assert fallback.read_text() == "ok"
    finally:
        Path.chmod = original_chmod  # type: ignore[method-assign]


def test_outcome_benchmark_real_evidence_gate_and_validation() -> None:
    records = [_outcome(index) for index in range(20)]
    report = run_outcome_benchmark(records)
    assert report["source"] == "outcome-ledger"
    assert report["overall"]["verified_samples"] == 20
    assert report["overall"]["verified_success_rate"] == 1.0
    assert report["release_gate"]["passed"] is True
    assert set(report["by_task_category"]) == {"bug_fix", "testing"}
    assert report["by_model"]["code-model"]["total"] == 20

    mixed = [
        _outcome(0, success=False, accepted=False),
        _outcome(1, safety=True),
        _outcome(2, accepted=None),
        OutcomeTask(
            task_id="pending",
            timestamp=datetime.now(UTC),
            repository_id="repo",
            request_hash="pending",
            task_category=TaskCategory.UNKNOWN,
        ),
    ]
    mixed.extend(
        [
            OutcomeTask(
                task_id="unverified",
                timestamp=datetime.now(UTC),
                repository_id="repo",
                request_hash="unverified",
                task_category=TaskCategory.BUG_FIX,
                execution_status="completed",
            ),
            _outcome(9, success=True, accepted=False),
        ]
    )
    failed = run_outcome_benchmark(mixed, minimum_verified_samples=1)
    assert failed["release_gate"]["passed"] is False
    assert failed["overall"]["verified_success_rate"] < 1
    assert failed["overall"]["safety_violation_rate"] > 0
    assert failed["by_capability_tier"]["unassigned"]["total"] == 2

    empty = run_outcome_benchmark([], minimum_verified_samples=1)
    assert empty["overall"]["verified_success_rate"] is None
    assert "INSUFFICIENT/FAIL" in empty["summary"]
    with pytest.raises(ValueError, match="at least 1"):
        run_outcome_benchmark([], minimum_verified_samples=0)
    with pytest.raises(ValueError, match="between 0 and 1"):
        run_outcome_benchmark([], minimum_verified_success_rate=1.1)
    with pytest.raises(ValueError, match="between 0 and 1"):
        run_outcome_benchmark([], maximum_safety_violation_rate=-0.1)


def test_cli_explain_metrics_and_outcome_benchmark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    main(["explain", "Where is app?", "--repo", str(tmp_path)])
    assert "What would make another route win" in capsys.readouterr().out
    main(["explain", "Where is app?", "--repo", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["decision"]["route"] == "local_fast"

    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {"state": "ready", "ready": True, "heartbeat_at": datetime.now(UTC).isoformat()}
        ),
        encoding="utf-8",
    )
    main(["metrics", "--status-file", str(status)])
    assert "tgv_ready 1" in capsys.readouterr().out
    main(["metrics", "--status-file", str(status), "--format", "json"])
    assert json.loads(capsys.readouterr().out)["ready"] is True
    with pytest.raises(SystemExit) as exc:
        main(["metrics", "--status-file", str(tmp_path / "missing.json")])
    assert exc.value.code == 2

    config = default_config()
    config.routing.use_local_model = False
    service = TGVService(
        config,
        Repository(tmp_path, config.repository, config.privacy.deny_patterns),
    )
    try:
        for index in range(2):
            task_id = service.create_outcome(f"Fix bug {index}")
            profile = service.profile(f"Fix bug {index}", retrieve=False)
            service.outcomes.record_routing(
                task_id,
                revised_profile=profile,
                confidence=0.9,
                model="code-model",
                capability_tier=CapabilityTier.STANDARD,
                reasons=["test"],
                files=[],
                symbols=[],
                context_token_estimate=10,
            )
            service.outcomes.record_execution(task_id, status="completed")
            service.outcomes.record_validation(
                task_id, tests_passed=True, static_checks_passed=True
            )
    finally:
        service.close()
    main(
        [
            "benchmark",
            "outcomes",
            "--repo",
            str(tmp_path),
            "--minimum-verified-samples",
            "1",
            "--json",
        ]
    )
    outcome_report = json.loads(capsys.readouterr().out)
    assert outcome_report["overall"]["verified_samples"] == 2
    assert outcome_report["release_gate"]["passed"] is True


def test_route_worker_shutdown_cancellation_and_argument_validation(tmp_path: Path) -> None:
    class AliveProcess:
        pid = 123

        def __init__(self) -> None:
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.alive = False

        def join(self, timeout: float) -> None:
            return

    class RequestQueue:
        def put(self, value: object) -> None:
            self.value = value

    class EmptyResponseQueue:
        def get(self, timeout: float) -> object:
            from queue import Empty

            raise Empty

    worker = RouteWorker.__new__(RouteWorker)
    worker._process = AliveProcess()
    worker._request_queue = RequestQueue()
    worker._response_queue = EmptyResponseQueue()
    worker.restarts = 0
    stopping = Event()
    stopping.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        worker.route("slow", 1, cancel_event=stopping, shutdown_grace_seconds=0)
    assert worker.restarts == 1
    assert worker._process is None

    invalid = RouteWorker.__new__(RouteWorker)
    with pytest.raises(ValueError, match="not be negative"):
        invalid.route("x", 1, shutdown_grace_seconds=-1)


def test_daemon_records_cancelled_request_and_draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    status = tmp_path / "cancelled.json"
    diagnostics = tmp_path / "cancelled.jsonl"

    class FakeWorker:
        pid = 777
        restarts = 1

        def __init__(self, service: TGVService) -> None:
            self.service = service

        def route(
            self,
            request: str,
            timeout_seconds: float,
            *,
            cancel_event: Event | None = None,
            shutdown_grace_seconds: float = 0.0,
        ) -> tuple[RouteDecision, dict[str, object]]:
            assert request == "cancel me"
            assert cancel_event is not None
            cancel_event.set()
            raise InterruptedError("shutdown")

        def close(self) -> None:
            return

    monkeypatch.setattr("tgv.daemon.RouteWorker", FakeWorker)
    output = io.StringIO()
    serve(
        service,
        timeout_seconds=1,
        status_file=status,
        diagnostics_file=diagnostics,
        heartbeat_interval=0.01,
        input_stream=io.StringIO('{"request":"cancel me"}\n'),
        output_stream=output,
        error_stream=io.StringIO(),
    )
    response = json.loads(output.getvalue())
    assert "shutdown" in response["error"]
    final = json.loads(status.read_text())
    assert final["requests_cancelled"] == 1
    assert final["requests_failed"] == 1
    event = json.loads(diagnostics.read_text())
    assert event["cancelled"] is True
    service.close()


def test_daemon_metrics_file_and_signal_draining(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = tmp_path / "daemon.json"
    metrics = tmp_path / "daemon.prom"

    class SignalEOF(io.StringIO):
        sent = False

        def readline(self, *args: object, **kwargs: object) -> str:
            if not self.sent:
                self.sent = True
                os.kill(os.getpid(), signal.SIGTERM)
            return ""

    serve(
        service,
        timeout_seconds=1,
        status_file=status,
        metrics_file=metrics,
        heartbeat_interval=0.01,
        shutdown_grace_seconds=0,
        input_stream=SignalEOF(),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
    )
    final = json.loads(status.read_text())
    assert final["state"] == "stopped"
    assert final["ready"] is False
    assert final["worker_active"] is False
    assert final["shutdown_signal"] == "SIGTERM"
    assert "tgv_up 0" in metrics.read_text()
    service.close()

    bad_service = _service(tmp_path / "bad")
    try:
        with pytest.raises(ValueError, match="not be negative"):
            serve(bad_service, timeout_seconds=1, shutdown_grace_seconds=-1)
    finally:
        bad_service.close()
