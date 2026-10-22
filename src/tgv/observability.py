from __future__ import annotations

import json
import math
import os
import time
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from tgv.models import RouteDecision


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist a small JSON status/metrics document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    _private_file(temporary)
    temporary.replace(path)
    _private_file(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one privacy-minimal operational event as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    _private_file(path)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


class RuntimeMetrics:
    """Thread-safe bounded operational metrics for routing and provider calls."""

    def __init__(self, *, latency_window: int = 256) -> None:
        self.started_at = utc_now()
        self._lock = Lock()
        self._route_latencies: deque[float] = deque(maxlen=latency_window)
        self._provider_latencies: deque[float] = deque(maxlen=latency_window)
        self._routes: Counter[str] = Counter()
        self._provider_calls: Counter[str] = Counter()
        self._provider_errors: Counter[str] = Counter()
        self._routing_total = 0
        self._routing_errors = 0

    def record_route(self, route: str, latency_ms: float) -> None:
        with self._lock:
            self._routing_total += 1
            self._routes[route] += 1
            self._route_latencies.append(latency_ms)

    def record_route_error(self, latency_ms: float) -> None:
        with self._lock:
            self._routing_total += 1
            self._routing_errors += 1
            self._route_latencies.append(latency_ms)

    def record_provider_call(
        self,
        provider: str,
        model: str,
        latency_ms: float,
        *,
        success: bool,
    ) -> None:
        key = f"{provider}:{model}"
        with self._lock:
            self._provider_calls[key] += 1
            if not success:
                self._provider_errors[key] += 1
            self._provider_latencies.append(latency_ms)

    @staticmethod
    def _latency_summary(values: deque[float]) -> dict[str, float]:
        sample = list(values)
        return {
            "sample_count": len(sample),
            "average": round(sum(sample) / len(sample), 3) if sample else 0.0,
            "p50": _percentile(sample, 0.50),
            "p95": _percentile(sample, 0.95),
            "max": round(max(sample), 3) if sample else 0.0,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            calls = dict(sorted(self._provider_calls.items()))
            errors = dict(sorted(self._provider_errors.items()))
            return {
                "started_at": self.started_at,
                "routing": {
                    "total": self._routing_total,
                    "errors": self._routing_errors,
                    "routes": dict(sorted(self._routes.items())),
                    "latency_ms": self._latency_summary(self._route_latencies),
                },
                "providers": {
                    "calls": calls,
                    "errors": errors,
                    "latency_ms": self._latency_summary(self._provider_latencies),
                },
            }


class RouteProvider(Protocol):
    def route(self, request: str) -> RouteDecision:
        ...


class ObservedProvider:
    """Decorate a local routing provider with latency/error accounting."""

    def __init__(
        self, provider: RouteProvider, metrics: RuntimeMetrics, *, name: str, model: str
    ) -> None:
        self.provider = provider
        self.metrics = metrics
        self.name = name
        self.model = model

    def route(self, request: str) -> RouteDecision:
        started = time.monotonic()
        try:
            result = self.provider.route(request)
        except Exception:
            self.metrics.record_provider_call(
                self.name,
                self.model,
                (time.monotonic() - started) * 1000,
                success=False,
            )
            raise
        self.metrics.record_provider_call(
            self.name,
            self.model,
            (time.monotonic() - started) * 1000,
            success=True,
        )
        return result

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()


def health_report(status_path: Path, *, maximum_age_seconds: float = 15.0) -> dict[str, Any]:
    """Evaluate daemon readiness from its heartbeat status document."""
    if maximum_age_seconds <= 0:
        raise ValueError("maximum_age_seconds must be greater than zero")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "healthy": False,
            "ready": False,
            "reason": f"status unavailable: {exc}",
            "status_file": str(status_path),
        }
    heartbeat = status.get("heartbeat_at")
    try:
        heartbeat_time = datetime.fromisoformat(str(heartbeat))
        if heartbeat_time.tzinfo is None:
            heartbeat_time = heartbeat_time.replace(tzinfo=UTC)
        age_seconds = max(0.0, (datetime.now(UTC) - heartbeat_time).total_seconds())
    except (TypeError, ValueError):
        age_seconds = float("inf")
    ready = bool(status.get("ready"))
    draining = bool(status.get("draining")) or status.get("state") == "draining"
    healthy = ready and not draining and age_seconds <= maximum_age_seconds
    if draining:
        reason = "daemon is draining"
    elif not ready:
        reason = "daemon is not ready"
    elif age_seconds > maximum_age_seconds:
        reason = f"heartbeat is stale ({age_seconds:.1f}s)"
    else:
        reason = "ready"
    return {
        "healthy": healthy,
        "ready": ready,
        "draining": draining,
        "state": status.get("state"),
        "reason": reason,
        "heartbeat_age_seconds": round(age_seconds, 3) if math.isfinite(age_seconds) else None,
        "status_file": str(status_path),
        "status": status,
    }


def _private_file(path: Path) -> None:
    """Best-effort owner-only permissions for local operational state."""
    try:
        path.chmod(0o600)
    except OSError:
        # Some platforms/filesystems do not implement POSIX modes.
        pass


def atomic_text(path: Path, content: str) -> None:
    """Atomically persist private text suitable for scraper/textfile collectors."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    _private_file(temporary)
    temporary.replace(path)
    _private_file(path)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(name: str, value: int | float, **labels: str) -> str:
    suffix = ""
    if labels:
        rendered = ",".join(
            f'{key}="{_label(str(item))}"' for key, item in sorted(labels.items())
        )
        suffix = "{" + rendered + "}"
    return f"{name}{suffix} {value}"


def prometheus_text(status: dict[str, Any]) -> str:
    """Render daemon status as Prometheus text exposition without prompt/repository content."""
    state = str(status.get("state", "ready" if status.get("ready") else "unknown"))
    lines = [
        "# HELP tgv_up Whether the daemon status is live enough to report.",
        "# TYPE tgv_up gauge",
        _metric("tgv_up", 0 if state == "stopped" else 1),
        "# HELP tgv_ready Whether the daemon is accepting new requests.",
        "# TYPE tgv_ready gauge",
        _metric("tgv_ready", int(bool(status.get("ready")))),
        "# HELP tgv_draining Whether graceful shutdown is in progress.",
        "# TYPE tgv_draining gauge",
        _metric("tgv_draining", int(bool(status.get("draining")))),
        "# TYPE tgv_requests_total counter",
        _metric("tgv_requests_total", int(status.get("requests_handled", 0)), result="handled"),
        _metric("tgv_requests_total", int(status.get("requests_succeeded", 0)), result="succeeded"),
        _metric("tgv_requests_total", int(status.get("requests_failed", 0)), result="failed"),
        _metric("tgv_requests_total", int(status.get("requests_timed_out", 0)), result="timed_out"),
        _metric("tgv_requests_total", int(status.get("requests_cancelled", 0)), result="cancelled"),
        "# TYPE tgv_worker_restarts_total counter",
        _metric("tgv_worker_restarts_total", int(status.get("worker_restarts", 0))),
    ]
    for route, count in sorted(dict(status.get("route_counts", {})).items()):
        lines.append(_metric("tgv_route_requests_total", int(count), route=str(route)))

    service = status.get("service_metrics")
    if isinstance(service, dict):
        routing = service.get("routing")
        if isinstance(routing, dict):
            lines.append(_metric("tgv_service_routing_total", int(routing.get("total", 0))))
            lines.append(_metric("tgv_service_routing_errors_total", int(routing.get("errors", 0))))
            latency = routing.get("latency_ms")
            if isinstance(latency, dict):
                for stat in ("average", "p50", "p95", "max"):
                    lines.append(
                        _metric(
                            "tgv_service_routing_latency_ms",
                            float(latency.get(stat, 0.0)),
                            statistic=stat,
                        )
                    )
        providers = service.get("providers")
        if isinstance(providers, dict):
            calls = providers.get("calls")
            if isinstance(calls, dict):
                for key, count in sorted(calls.items()):
                    provider, _, model = str(key).partition(":")
                    lines.append(
                        _metric(
                            "tgv_provider_calls_total",
                            int(count),
                            provider=provider,
                            model=model or "unknown",
                        )
                    )
            errors = providers.get("errors")
            if isinstance(errors, dict):
                for key, count in sorted(errors.items()):
                    provider, _, model = str(key).partition(":")
                    lines.append(
                        _metric(
                            "tgv_provider_errors_total",
                            int(count),
                            provider=provider,
                            model=model or "unknown",
                        )
                    )
    return "\n".join(lines) + "\n"


def write_prometheus(path: Path, status: dict[str, Any]) -> None:
    atomic_text(path, prometheus_text(status))
