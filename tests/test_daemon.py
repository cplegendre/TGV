import io
import json

from tgv.daemon import _atomic_status, serve
from tgv.models import Route, RouteDecision


class FakeService:
    def route(self, request: str) -> RouteDecision:
        if request == "slow":
            return RouteDecision(route=Route.LOCAL_FAST, confidence=1, rationale="ok")
        return RouteDecision(route=Route.LOCAL_FAST, confidence=1, rationale="ok")


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


def test_serve_timeout(monkeypatch) -> None:
    ticks = iter([0.0, 2.0, 2.1])
    monkeypatch.setattr("tgv.daemon.time.monotonic", lambda: next(ticks))
    stdout = io.StringIO()
    serve(
        FakeService(),  # type: ignore[arg-type]
        timeout_seconds=1,
        input_stream=io.StringIO('{"request":"slow"}\n'),
        output_stream=stdout,
        error_stream=io.StringIO(),
    )
    assert "exceeded" in json.loads(stdout.getvalue())["error"]
