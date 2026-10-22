import io
import json
import time

import pytest

from tgv.daemon import _atomic_status, _route_with_timeout, serve
from tgv.models import Route, RouteDecision


class FakeService:
    def route(self, request: str) -> RouteDecision:
        return RouteDecision(route=Route.LOCAL_FAST, confidence=1, rationale="ok")


class SlowService:
    def route(self, request: str) -> RouteDecision:
        time.sleep(0.08)
        return RouteDecision(route=Route.LOCAL_FAST, confidence=1, rationale="late")


class BrokenService:
    def route(self, request: str) -> RouteDecision:
        raise ValueError("broken route")


def test_atomic_status_and_sequential_serve(tmp_path) -> None:
    status_path = tmp_path / "run" / "status.json"
    _atomic_status(status_path, {"pid": 1})
    assert json.loads(status_path.read_text()) == {"pid": 1}

    stdin = io.StringIO('{"request":"where"}\nnot-json\n{"request": 4}\n')
    stdout = io.StringIO()
    stderr = io.StringIO()
    serve(
        FakeService(),  # type: ignore[arg-type]
        timeout_seconds=10,
        status_file=status_path,
        input_stream=stdin,
        output_stream=stdout,
        error_stream=stderr,
    )
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    logs = [json.loads(line) for line in stderr.getvalue().splitlines()]
    assert responses[0]["decision"]["route"] == "local_fast"
    assert "error" in responses[1]
    assert "error" in responses[2]
    assert all({"request_id", "route", "latency_ms", "error"} <= row.keys() for row in logs)
    status = json.loads(status_path.read_text())
    assert status["requests_handled"] == 3
    assert status["last_request_at"]


def test_serve_timeout_interrupts_slow_route() -> None:
    stdout = io.StringIO()
    started = time.monotonic()
    serve(
        SlowService(),  # type: ignore[arg-type]
        timeout_seconds=0.01,
        input_stream=io.StringIO('{"request":"slow"}\n'),
        output_stream=stdout,
        error_stream=io.StringIO(),
    )
    elapsed = time.monotonic() - started
    assert "exceeded" in json.loads(stdout.getvalue())["error"]
    assert elapsed < 0.07


def test_route_timeout_nonpositive_and_thread_fallback(monkeypatch) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        _route_with_timeout(FakeService(), "x", 0)  # type: ignore[arg-type]

    monkeypatch.setattr("tgv.daemon.current_thread", lambda: object())
    monkeypatch.setattr("tgv.daemon.main_thread", lambda: object())
    result = _route_with_timeout(FakeService(), "x", 1)  # type: ignore[arg-type]
    assert result.route == Route.LOCAL_FAST
    with pytest.raises(ValueError, match="broken route"):
        _route_with_timeout(BrokenService(), "x", 1)  # type: ignore[arg-type]
    with pytest.raises(TimeoutError, match="exceeded"):
        _route_with_timeout(SlowService(), "x", 0.001)  # type: ignore[arg-type]
