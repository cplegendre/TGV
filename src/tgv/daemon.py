from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import TextIO
from uuid import uuid4

from tgv.service import TGVService


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_status(path: Path, status: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


class _RequestDeadline:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.previous = None

    def __enter__(self) -> None:
        if not hasattr(signal, "SIGALRM"):
            raise RuntimeError("request timeouts require SIGALRM")
        self.previous = signal.getsignal(signal.SIGALRM)

        def timeout(_signum: int, _frame: object) -> None:
            raise TimeoutError(f"request exceeded {self.seconds:g}s timeout")

        signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous)


def serve(
    service: TGVService,
    *,
    timeout_seconds: float,
    status_file: Path | None = None,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    error_stream: TextIO = sys.stderr,
) -> None:
    """Serve sequential JSONL requests; one request is in flight at a time."""
    stopping = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    status: dict[str, object] = {
        "pid": os.getpid(),
        "started_at": _now(),
        "last_request_at": None,
        "requests_handled": 0,
    }
    requests_handled = 0
    if status_file is not None:
        _atomic_status(status_file, status)
    try:
        while not stopping.is_set():
            line = input_stream.readline()
            if not line:
                break
            request_id = str(uuid4())
            started = time.monotonic()
            route: str | None = None
            error: str | None = None
            try:
                payload = json.loads(line)
                request = payload["request"]
                if not isinstance(request, str):
                    raise ValueError("request must be a string")
                with _RequestDeadline(timeout_seconds):
                    decision = service.route(request)
                route = decision.route.value
                print(
                    json.dumps(
                        {"request_id": request_id, "decision": decision.model_dump(mode="json")}
                    ),
                    file=output_stream,
                    flush=True,
                )
            except (KeyError, TypeError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
                error = str(exc)
                print(
                    json.dumps({"request_id": request_id, "error": error}),
                    file=output_stream,
                    flush=True,
                )
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            print(
                json.dumps(
                    {
                        "request_id": request_id,
                        "route": route,
                        "latency_ms": latency_ms,
                        "error": error,
                    },
                    sort_keys=True,
                ),
                file=error_stream,
                flush=True,
            )
            status["last_request_at"] = _now()
            requests_handled += 1
            status["requests_handled"] = requests_handled
            if status_file is not None:
                _atomic_status(status_file, status)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
