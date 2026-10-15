from __future__ import annotations

import io
import json
import queue
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgv.cli import main
from tgv.config import default_config
from tgv.daemon import RouteWorker, _worker_main, serve
from tgv.evaluation import default_cases_path, run_benchmark
from tgv.models import Route, RouteDecision
from tgv.observability import (
    ObservedProvider,
    RuntimeMetrics,
    append_jsonl,
    atomic_json,
    health_report,
    utc_now,
)
from tgv.repository import Repository
from tgv.service import TGVService


class GoodProvider:
    def __init__(self) -> None:
        self.closed = False

    def route(self, request: str) -> RouteDecision:
        return RouteDecision(route=Route.LOCAL_FAST, confidence=1, rationale=request)

    def close(self) -> None:
        self.closed = True


class BadProvider:
    def route(self, request: str) -> RouteDecision:
        raise RuntimeError(request)


def _service(tmp_path: Path) -> TGVService:
    (tmp_path / "app.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    config = default_config()
    config.routing.use_local_model = False
    config.intelligence.enabled = False
    repo = Repository(tmp_path, config.repository, config.privacy.deny_patterns)
    return TGVService(config, repo)


def test_runtime_metrics_observed_provider_and_persistence(tmp_path: Path) -> None:
    metrics = RuntimeMetrics(latency_window=2)
    metrics.record_route("local_fast", 10)
    metrics.record_route("local_code", 20)
    metrics.record_route("local_code", 30)
    metrics.record_route_error(40)
    good = GoodProvider()
    observed = ObservedProvider(good, metrics, name="fake", model="one")
    assert observed.route("ok").route == Route.LOCAL_FAST
    observed.close()
    assert good.closed is True

    broken = ObservedProvider(BadProvider(), metrics, name="fake", model="bad")
    with pytest.raises(RuntimeError, match="boom"):
        broken.route("boom")
    broken.close()

    snapshot = metrics.snapshot()
    assert snapshot["routing"]["total"] == 4
    assert snapshot["routing"]["errors"] == 1
    assert snapshot["routing"]["latency_ms"]["sample_count"] == 2
    assert snapshot["routing"]["latency_ms"]["p95"] == 40
    assert snapshot["providers"]["calls"] == {"fake:bad": 1, "fake:one": 1}
    assert snapshot["providers"]["errors"] == {"fake:bad": 1}

    status = tmp_path / "ops" / "status.json"
    events = tmp_path / "ops" / "events.jsonl"
    atomic_json(status, {"ready": True})
    append_jsonl(events, {"event": "one"})
    append_jsonl(events, {"event": "two"})
    assert json.loads(status.read_text()) == {"ready": True}
    assert len(events.read_text().splitlines()) == 2
    assert utc_now().endswith("+00:00")


def test_health_report_ready_stale_stopped_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    with pytest.raises(ValueError, match="greater than zero"):
        health_report(path, maximum_age_seconds=0)

    missing = health_report(path)
    assert missing["healthy"] is False
    assert missing["ready"] is False

    atomic_json(path, {"ready": True, "heartbeat_at": datetime.now(UTC).isoformat()})
    ready = health_report(path, maximum_age_seconds=5)
    assert ready["healthy"] is True
    assert ready["reason"] == "ready"

    old = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    atomic_json(path, {"ready": True, "heartbeat_at": old})
    stale = health_report(path, maximum_age_seconds=1)
    assert stale["healthy"] is False
    assert "stale" in stale["reason"]

    atomic_json(path, {"ready": False, "heartbeat_at": datetime.now(UTC).isoformat()})
    stopped = health_report(path)
    assert stopped["healthy"] is False
    assert stopped["reason"] == "daemon is not ready"

    atomic_json(path, {"ready": True, "heartbeat_at": "not-a-date"})
    malformed = health_report(path)
    assert malformed["heartbeat_age_seconds"] is None


def test_worker_main_success_and_error_paths(tmp_path: Path) -> None:
    service = _service(tmp_path)
    payload = service.config.model_dump(mode="json")
    service.close()
    requests: queue.Queue[object] = queue.Queue()
    responses: queue.Queue[dict[str, object]] = queue.Queue()
    requests.put(("good", "Where is helper?"))
    requests.put(("bad", None))
    requests.put(None)
    _worker_main(payload, str(tmp_path), False, requests, responses)
    ready = responses.get_nowait()
    good = responses.get_nowait()
    bad = responses.get_nowait()
    assert ready["type"] == "ready"
    assert good["ok"] is True
    assert good["service_metrics"]
    assert bad["ok"] is False
    assert bad["error_type"] in {"TypeError", "AttributeError"}


def test_route_worker_restart_and_timeout_path(tmp_path: Path) -> None:
    service = _service(tmp_path)
    worker = RouteWorker(service)
    try:
        decision, metrics = worker.route("Where is helper?", 10)
        assert decision.route == Route.LOCAL_FAST
        assert metrics["routing"]["total"] == 1
        first_pid = worker.pid
        worker.restart()
        assert worker.restarts == 1
        assert worker.pid != first_pid
        recovered, _ = worker.route("Where is helper?", 10)
        assert recovered.route == Route.LOCAL_FAST
    finally:
        worker.close()
        worker.close()
        service.close()

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
            raise queue.Empty

    timed = RouteWorker.__new__(RouteWorker)
    timed._process = AliveProcess()
    timed._request_queue = RequestQueue()
    timed._response_queue = EmptyResponseQueue()
    timed.restarts = 0

    def restarted() -> None:
        timed.restarts += 1

    timed.restart = restarted  # type: ignore[method-assign]
    with pytest.raises(TimeoutError, match="exceeded"):
        timed.route("slow", 0.01)
    assert timed.restarts == 1


def test_actual_daemon_writes_metrics_diagnostics_and_heartbeat(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = tmp_path / "status.json"
    diagnostics = tmp_path / "diagnostics.jsonl"

    class DelayedEOF(io.StringIO):
        def readline(self, *args: object, **kwargs: object) -> str:
            value = super().readline(*args, **kwargs)
            if not value:
                time.sleep(0.02)
            return value

    stdout = io.StringIO()
    stderr = io.StringIO()
    serve(
        service,
        timeout_seconds=5,
        status_file=status,
        diagnostics_file=diagnostics,
        heartbeat_interval=0.005,
        input_stream=DelayedEOF('{"request":"Where is helper?"}\n'),
        output_stream=stdout,
        error_stream=stderr,
    )
    response = json.loads(stdout.getvalue())
    assert response["decision"]["route"] == "local_fast"
    log = json.loads(stderr.getvalue())
    assert log["timed_out"] is False
    final_status = json.loads(status.read_text())
    assert final_status["ready"] is False
    assert final_status["requests_succeeded"] == 1
    assert final_status["route_counts"] == {"local_fast": 1}
    assert final_status["worker_pid"]
    assert final_status["service_metrics"]["routing"]["total"] == 1
    event = json.loads(diagnostics.read_text())
    assert event["route"] == "local_fast"
    service.close()


def test_daemon_rejects_bad_heartbeat_interval(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="heartbeat_interval"):
        serve(service, timeout_seconds=1, heartbeat_interval=0, input_stream=io.StringIO())
    service.close()


def test_service_provider_metrics_are_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = GoodProvider()
    monkeypatch.setattr("tgv.service.create_local_provider", lambda config: fake)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    config = default_config()
    config.intelligence.enabled = False
    service = TGVService(
        config, Repository(tmp_path, config.repository, config.privacy.deny_patterns)
    )
    service.route("Where is app?")
    snapshot = service.metrics.snapshot()
    provider_key = f"{config.models['fast'].provider}:{config.models['fast'].model}"
    assert snapshot["providers"]["calls"] == {provider_key: 1}
    service.close()
    assert fake.closed is True


def test_broader_intelligence_benchmark_and_release_gate(tmp_path: Path) -> None:
    report = run_benchmark(default_cases_path())
    assert report["total_cases"] == 104
    assert report["release_gate"]["passed"] is True
    assert report["task_category_accuracy"] >= 0.90
    assert report["complexity_band_accuracy"] >= 0.90
    assert report["acceptable_route_accuracy"] >= 0.95
    assert report["false_low_risk_rate"] == 0
    assert report["distribution"]["complexity_band"] == {"high": 16, "low": 32, "medium": 56}

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    failed = run_benchmark(empty)
    assert failed["release_gate"]["passed"] is False
    assert not all(failed["release_gate"]["checks"].values())

    duplicate = tmp_path / "duplicate.jsonl"
    row = {
        "case_id": "same",
        "request": "Where is app?",
        "expected_task_category": "repository_investigation",
        "expected_complexity_band": "low",
        "acceptable_routes": ["local_fast"],
    }
    duplicate.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        run_benchmark(duplicate)

    bad_band = tmp_path / "band.jsonl"
    row["case_id"] = "band"
    row["expected_complexity_band"] = "tiny"
    bad_band.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="complexity band"):
        run_benchmark(bad_band)

    bad_routes = tmp_path / "routes.jsonl"
    row["case_id"] = "routes"
    row["expected_complexity_band"] = "low"
    row["acceptable_routes"] = []
    bad_routes.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="acceptable_routes"):
        run_benchmark(bad_routes)


def test_health_cli_and_benchmark_enforcement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = tmp_path / "status.json"
    atomic_json(status, {"ready": True, "heartbeat_at": datetime.now(UTC).isoformat()})
    main(["health", "--status-file", str(status), "--json"])
    assert json.loads(capsys.readouterr().out)["healthy"] is True

    atomic_json(status, {"ready": False, "heartbeat_at": datetime.now(UTC).isoformat()})
    with pytest.raises(SystemExit) as exc:
        main(["health", "--status-file", str(status)])
    assert exc.value.code == 1
    assert "health: fail" in capsys.readouterr().out

    tiny = tmp_path / "tiny.jsonl"
    tiny.write_text(
        json.dumps(
            {
                "case_id": "one",
                "request": "Where is app?",
                "expected_task_category": "repository_investigation",
                "expected_complexity_band": "low",
                "acceptable_routes": ["local_fast"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        main(["benchmark", "run", "--cases", str(tiny), "--enforce"])
    assert exc.value.code == 1
    assert "gate=FAIL" in capsys.readouterr().out


def test_route_worker_edge_paths_without_child_process(tmp_path: Path) -> None:
    class FakeProcess:
        def __init__(self, alive: bool = True, *, stubborn: bool = False) -> None:
            self.pid = 444
            self.alive = alive
            self.stubborn = stubborn
            self.terminated = False
            self.killed = False
            self.joined = 0

        def is_alive(self) -> bool:
            return self.alive if not self.stubborn else not self.killed

        def terminate(self) -> None:
            self.terminated = True
            if not self.stubborn:
                self.alive = False

        def kill(self) -> None:
            self.killed = True
            self.alive = False

        def join(self, timeout: float) -> None:
            self.joined += 1

    blank = RouteWorker.__new__(RouteWorker)
    blank._process = None
    blank._terminate()
    assert blank.pid is None

    stopped = FakeProcess(alive=False)
    blank._process = stopped
    blank._terminate()
    assert stopped.joined == 1

    stubborn = FakeProcess(stubborn=True)
    blank._process = stubborn
    blank._terminate()
    assert stubborn.terminated is True
    assert stubborn.killed is True

    with pytest.raises(ValueError, match="greater than zero"):
        timed = RouteWorker.__new__(RouteWorker)
        timed.route("x", 0)

    class RequestQueue:
        def __init__(self) -> None:
            self.value: tuple[str, str] | None = None

        def put(self, value: tuple[str, str], timeout: float | None = None) -> None:
            self.value = value

    request_queue = RequestQueue()

    class MatchingResponse:
        def get(self, timeout: float) -> dict[str, object]:
            assert request_queue.value is not None
            request_id, _request = request_queue.value
            return {
                "request_id": request_id,
                "ok": True,
                "decision": {
                    "route": "local_fast",
                    "confidence": 1.0,
                    "rationale": "fake",
                },
            }

    routed = RouteWorker.__new__(RouteWorker)
    routed._process = FakeProcess(alive=False)
    routed._request_queue = request_queue
    routed._response_queue = MatchingResponse()
    routed.restarts = 0

    def make_alive() -> None:
        routed.restarts += 1
        routed._process = FakeProcess(alive=True)

    routed.restart = make_alive  # type: ignore[method-assign]
    decision, metrics = routed.route("x", 1)
    assert decision.route == Route.LOCAL_FAST
    assert metrics == {}
    assert routed.restarts == 1

    class MismatchResponse:
        def get(self, timeout: float) -> dict[str, object]:
            return {"request_id": "wrong", "ok": True, "decision": {}}

    routed._response_queue = MismatchResponse()
    with pytest.raises(RuntimeError, match="mismatched"):
        routed.route("x", 1)

    class ErrorResponse:
        def get(self, timeout: float) -> dict[str, object]:
            assert request_queue.value is not None
            return {
                "request_id": request_queue.value[0],
                "ok": False,
                "error_type": "Boom",
                "error": "failed",
            }

    routed._response_queue = ErrorResponse()
    with pytest.raises(RuntimeError, match="Boom: failed"):
        routed.route("x", 1)

    class FailingCloseQueue:
        def put(self, value: object, timeout: float | None = None) -> None:
            raise RuntimeError("closed")

    close_worker = RouteWorker.__new__(RouteWorker)
    close_worker._process = FakeProcess(alive=True)
    close_worker._request_queue = FailingCloseQueue()
    close_worker._response_queue = None
    close_worker._terminate = lambda: None  # type: ignore[method-assign]
    close_worker.close()
    assert close_worker._process is None


def test_route_worker_context_manager_and_naive_health_timestamp(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with RouteWorker(service) as worker:
        assert worker.pid is not None
    service.close()

    status = tmp_path / "naive.json"
    atomic_json(status, {"ready": True, "heartbeat_at": datetime.now().isoformat()})
    assert health_report(status, maximum_age_seconds=5)["healthy"] is True
