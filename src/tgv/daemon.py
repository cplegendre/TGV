from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, current_thread, main_thread
from typing import Any, TextIO
from uuid import uuid4

from tgv.models import RouteDecision, TGVConfig
from tgv.observability import append_jsonl, atomic_json, write_prometheus
from tgv.repository import Repository
from tgv.service import TGVService


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_status(path: Path, status: dict[str, object]) -> None:
    atomic_json(path, status)


def _route_with_timeout(
    service: Any, request: str, timeout_seconds: float
) -> RouteDecision:
    """Compatibility helper for lightweight/in-process service implementations.

    The production TGV daemon uses :class:`RouteWorker`, whose process can be
    terminated after a deadline on every supported platform. This helper remains
    useful for embedded callers and test doubles that are not reconstructable.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    if (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and current_thread() is main_thread()
    ):
        sigalrm = signal.SIGALRM
        previous_handler = signal.getsignal(sigalrm)

        def expire(_signum: int, _frame: object) -> None:
            raise TimeoutError(f"request exceeded {timeout_seconds:g}s timeout")

        signal.signal(sigalrm, expire)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            return service.route(request)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(sigalrm, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])

    results: Queue[tuple[bool, RouteDecision | Exception]] = Queue(maxsize=1)

    def run() -> None:
        try:
            results.put((True, service.route(request)))
        except Exception as exc:
            results.put((False, exc))

    Thread(target=run, daemon=True, name="tgv-request").start()
    try:
        ok, value = results.get(timeout=timeout_seconds)
    except Empty as exc:
        raise TimeoutError(f"request exceeded {timeout_seconds:g}s timeout") from exc
    if ok:
        assert isinstance(value, RouteDecision)
        return value
    assert isinstance(value, Exception)
    raise value


def _worker_main(
    config_payload: dict[str, Any],
    repository_root: str,
    read_only: bool,
    request_queue: Any,
    response_queue: Any,
) -> None:
    """Own a reconstructable TGVService inside an isolated worker process."""
    config = TGVConfig.model_validate(config_payload)
    root = Path(repository_root)
    repository = Repository(
        root,
        config.repository,
        config.privacy.deny_patterns,
        persist_index=not read_only,
    )
    service = TGVService(config, repository, read_only=read_only)
    response_queue.put({"type": "ready", "pid": os.getpid()})
    try:
        while True:
            item = request_queue.get()
            if item is None:
                return
            request_id, request = item
            try:
                decision = service.route(request)
                response_queue.put(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": True,
                        "decision": decision.model_dump(mode="json"),
                        "service_metrics": service.metrics.snapshot(),
                    }
                )
            except Exception as exc:
                response_queue.put(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "service_metrics": service.metrics.snapshot(),
                    }
                )
    finally:
        service.close()


class RouteWorker:
    """Persistent, restartable process used for cancellable daemon routing.

    A timed-out request terminates the entire worker process, which guarantees that
    the request cannot keep consuming CPU/network resources in a background thread.
    The worker is then reconstructed from validated configuration for the next call.
    """

    def __init__(self, service: TGVService, *, context: Any | None = None) -> None:
        self.config_payload = service.config.model_dump(mode="json")
        self.repository_root = str(service.repository.root)
        self.read_only = service.read_only
        # ``spawn`` is intentionally used on every platform. It avoids the
        # fork-after-threads deadlock class and gives Windows/POSIX identical
        # worker lifecycle semantics. Worker startup happens once per daemon.
        self.context = context or multiprocessing.get_context("spawn")
        self.restarts = 0
        self._process: Any | None = None
        self._request_queue: Any | None = None
        self._response_queue: Any | None = None
        self._start()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def _start(self) -> None:
        self._request_queue = self.context.Queue(maxsize=1)
        self._response_queue = self.context.Queue(maxsize=1)
        self._process = self.context.Process(
            target=_worker_main,
            args=(
                self.config_payload,
                self.repository_root,
                self.read_only,
                self._request_queue,
                self._response_queue,
            ),
            name="tgv-route-worker",
            daemon=True,
        )
        self._process.start()
        try:
            ready = self._response_queue.get(timeout=10.0)
        except Empty as exc:
            self._terminate()
            self._process = None
            raise RuntimeError("routing worker failed to become ready within 10s") from exc
        if ready.get("type") != "ready":
            self._terminate()
            self._process = None
            raise RuntimeError("routing worker returned an invalid startup handshake")

    def _terminate(self) -> None:
        process = self._process
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=1.0)
        else:
            process.join(timeout=0.1)

    def restart(self) -> None:
        self._terminate()
        self.restarts += 1
        self._start()

    def _abandon(self) -> None:
        self._terminate()
        self.restarts += 1
        self._process = None
        self._request_queue = None
        self._response_queue = None

    def route(
        self,
        request: str,
        timeout_seconds: float,
        *,
        cancel_event: Event | None = None,
        shutdown_grace_seconds: float = 0.0,
    ) -> tuple[RouteDecision, dict[str, Any]]:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds must not be negative")
        if self._process is None:
            self._start()
        elif not self._process.is_alive():
            self.restart()
        assert self._request_queue is not None
        assert self._response_queue is not None
        request_id = str(uuid4())
        self._request_queue.put((request_id, request))
        deadline = time.monotonic() + timeout_seconds
        cancel_deadline: float | None = None
        while True:
            now = time.monotonic()
            if cancel_event is not None and cancel_event.is_set():
                if cancel_deadline is None:
                    cancel_deadline = now + shutdown_grace_seconds
                if now >= cancel_deadline:
                    self._abandon()
                    raise InterruptedError("request cancelled during daemon shutdown")
            remaining = deadline - now
            if remaining <= 0:
                self._abandon()
                raise TimeoutError(f"request exceeded {timeout_seconds:g}s timeout")
            wait_seconds = min(remaining, 0.1)
            if cancel_deadline is not None:
                wait_seconds = min(wait_seconds, max(0.001, cancel_deadline - now))
            try:
                response = self._response_queue.get(timeout=wait_seconds)
            except Empty:
                continue
            if response.get("request_id") != request_id:
                self.restart()
                raise RuntimeError("routing worker returned a mismatched request id")
            metrics = response.get("service_metrics", {})
            if not response.get("ok"):
                error_type = response.get("error_type", "RouteWorkerError")
                error = response.get("error", "unknown worker failure")
                raise RuntimeError(f"{error_type}: {error}")
            return RouteDecision.model_validate(response["decision"]), metrics

    def close(self) -> None:
        if self._process is None:
            return
        if self._process.is_alive() and self._request_queue is not None:
            try:
                self._request_queue.put(None, timeout=0.1)
            except Exception:
                pass
            self._process.join(timeout=1.0)
        self._terminate()
        self._process = None

    def __enter__(self) -> RouteWorker:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def serve(
    service: TGVService,
    *,
    timeout_seconds: float,
    status_file: Path | None = None,
    diagnostics_file: Path | None = None,
    metrics_file: Path | None = None,
    heartbeat_interval: float = 5.0,
    shutdown_grace_seconds: float = 10.0,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    error_stream: TextIO = sys.stderr,
) -> None:
    """Serve sequential JSONL requests with cancellable work and graceful draining."""
    if heartbeat_interval <= 0:
        raise ValueError("heartbeat_interval must be greater than zero")
    if shutdown_grace_seconds < 0:
        raise ValueError("shutdown_grace_seconds must not be negative")
    stopping = Event()
    finished = Event()
    status_lock = Lock()
    input_queue: Queue[str | None] = Queue(maxsize=1)
    shutdown_signal: dict[str, str | None] = {"name": None}

    def stop(signum: int, _frame: object) -> None:
        try:
            shutdown_signal["name"] = signal.Signals(signum).name
        except ValueError:
            shutdown_signal["name"] = str(signum)
        stopping.set()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    status: dict[str, object] = {
        "pid": os.getpid(),
        "worker_pid": None,
        "worker_restarts": 0,
        "worker_active": False,
        "started_at": _now(),
        "heartbeat_at": _now(),
        "last_request_at": None,
        "requests_handled": 0,
        "requests_succeeded": 0,
        "requests_failed": 0,
        "requests_timed_out": 0,
        "requests_cancelled": 0,
        "route_counts": {},
        "last_error": None,
        "ready": False,
        "draining": False,
        "state": "starting",
        "shutdown_signal": None,
        "service_metrics": {},
    }

    def persist_status() -> None:
        with status_lock:
            snapshot = dict(status)
        if status_file is not None:
            _atomic_status(status_file, snapshot)
        if metrics_file is not None:
            write_prometheus(metrics_file, snapshot)

    def heartbeat() -> None:
        while not finished.wait(heartbeat_interval):
            with status_lock:
                status["heartbeat_at"] = _now()
                if stopping.is_set():
                    status["ready"] = False
                    status["draining"] = True
                    status["state"] = "draining"
                    status["shutdown_signal"] = shutdown_signal["name"]
            persist_status()

    def read_input() -> None:
        while not stopping.is_set():
            line = input_stream.readline()
            item: str | None = line if line else None
            while not stopping.is_set():
                try:
                    input_queue.put(item, timeout=0.1)
                    break
                except Full:
                    continue
            if item is None:
                return

    persist_status()
    heartbeat_thread: Thread | None = None
    input_thread: Thread | None = None
    worker: RouteWorker | None = None

    try:
        worker = RouteWorker(service) if isinstance(service, TGVService) else None
        with status_lock:
            status["worker_pid"] = worker.pid if worker is not None else None
            status["worker_active"] = worker is not None
            status["ready"] = True
            status["state"] = "ready"
            status["heartbeat_at"] = _now()
        persist_status()
        if status_file is not None or metrics_file is not None:
            heartbeat_thread = Thread(target=heartbeat, daemon=True, name="tgv-heartbeat")
            heartbeat_thread.start()
        input_thread = Thread(target=read_input, daemon=True, name="tgv-input-reader")
        input_thread.start()

        while not stopping.is_set():
            try:
                line = input_queue.get(timeout=0.1)
            except Empty:
                continue
            if line is None or stopping.is_set():
                break
            request_id = str(uuid4())
            started = time.monotonic()
            route: str | None = None
            error: str | None = None
            error_type: str | None = None
            timed_out = False
            cancelled = False
            service_metrics: dict[str, Any] = {}
            try:
                payload = json.loads(line)
                request = payload["request"]
                if not isinstance(request, str):
                    raise ValueError("request must be a string")
                if worker is not None:
                    decision, service_metrics = worker.route(
                        request,
                        timeout_seconds,
                        cancel_event=stopping,
                        shutdown_grace_seconds=shutdown_grace_seconds,
                    )
                else:
                    decision = _route_with_timeout(service, request, timeout_seconds)
                route = decision.route.value
                print(
                    json.dumps(
                        {"request_id": request_id, "decision": decision.model_dump(mode="json")}
                    ),
                    file=output_stream,
                    flush=True,
                )
            except Exception as exc:
                error = str(exc)
                error_type = type(exc).__name__
                timed_out = isinstance(exc, TimeoutError)
                cancelled = isinstance(exc, InterruptedError)
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
                        "error_type": error_type,
                        "timed_out": timed_out,
                        "cancelled": cancelled,
                    },
                    sort_keys=True,
                ),
                file=error_stream,
                flush=True,
            )
            with status_lock:
                status["last_request_at"] = _now()
                status["heartbeat_at"] = _now()
                status["requests_handled"] = int(status["requests_handled"]) + 1
                status["last_error"] = error
                status["service_metrics"] = service_metrics
                if route is not None:
                    status["requests_succeeded"] = int(status["requests_succeeded"]) + 1
                    route_counts = dict(status["route_counts"])  # type: ignore[arg-type]
                    route_counts[route] = int(route_counts.get(route, 0)) + 1
                    status["route_counts"] = route_counts
                else:
                    status["requests_failed"] = int(status["requests_failed"]) + 1
                if timed_out:
                    status["requests_timed_out"] = int(status["requests_timed_out"]) + 1
                if cancelled:
                    status["requests_cancelled"] = int(status["requests_cancelled"]) + 1
                if worker is not None:
                    status["worker_pid"] = worker.pid
                    status["worker_restarts"] = worker.restarts
                if stopping.is_set():
                    status["ready"] = False
                    status["draining"] = True
                    status["state"] = "draining"
                    status["shutdown_signal"] = shutdown_signal["name"]
            persist_status()
            if diagnostics_file is not None:
                append_jsonl(
                    diagnostics_file,
                    {
                        "timestamp": _now(),
                        "request_id": request_id,
                        "route": route,
                        "latency_ms": latency_ms,
                        "error_type": error_type,
                        "timed_out": timed_out,
                        "cancelled": cancelled,
                        "worker_restarts": worker.restarts if worker is not None else 0,
                    },
                )
    finally:
        stopping.set()
        if worker is not None:
            worker.close()
        with status_lock:
            status["ready"] = False
            status["draining"] = False
            status["state"] = "stopped"
            status["shutdown_signal"] = shutdown_signal["name"]
            status["worker_active"] = False
            status["stopped_at"] = _now()
            status["heartbeat_at"] = _now()
        finished.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=max(heartbeat_interval * 2, 0.1))
        if input_thread is not None:
            input_thread.join(timeout=0.1)
        persist_status()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
