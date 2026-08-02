from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tgv.exceptions import TGVError


class TERUnavailableError(TGVError):
    """Raised when the optional TER integration is requested but unavailable."""


@dataclass(frozen=True)
class ScoredSession:
    route: str
    aggregate_ter: float
    aligned_tokens: int
    waste_tokens: int


def _ter_analyzer() -> Callable[[Path], Any]:
    try:
        from ter_calculator.analyze_pipeline import analyze_session, default_analyze_args
    except ImportError as exc:
        raise TERUnavailableError(
            "TER support is not installed. Install it with: pip install 'tgv-agent[ter]'"
        ) from exc

    def analyze(path: Path) -> Any:
        return analyze_session(default_analyze_args(str(path)))

    return analyze


def _route_from_log(path: Path) -> str:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            route = entry.get("route_decision", {}).get("route")
            if isinstance(route, str):
                return route
    raise ValueError(f"No route_decision metadata found in {path}")


def score_sessions(
    log_directory: Path,
    analyzer: Callable[[Path], Any] | None = None,
) -> list[ScoredSession]:
    analyze = analyzer or _ter_analyzer()
    return [
        ScoredSession(
            route=_route_from_log(path),
            aggregate_ter=float(result.aggregate_ter),
            aligned_tokens=int(result.aligned_tokens),
            waste_tokens=int(result.waste_tokens),
        )
        for path in sorted(log_directory.glob("*.jsonl"))
        for result in [analyze(path)]
    ]


def aggregate_by_route(sessions: list[ScoredSession]) -> dict[str, dict[str, int | float]]:
    grouped: dict[str, list[ScoredSession]] = defaultdict(list)
    for session in sessions:
        grouped[session.route].append(session)

    report: dict[str, dict[str, int | float]] = {}
    for route, items in sorted(grouped.items()):
        aligned = sum(item.aligned_tokens for item in items)
        waste = sum(item.waste_tokens for item in items)
        total = aligned + waste
        weighted_ter = (
            sum(item.aggregate_ter * (item.aligned_tokens + item.waste_tokens) for item in items)
            / total
            if total
            else 1.0
        )
        report[route] = {
            "aggregate_ter": round(weighted_ter, 4),
            "aligned_tokens": aligned,
            "waste_tokens": waste,
            "session_count": len(items),
        }
    return report


@dataclass(frozen=True)
class CalibrationProposal:
    route: str
    threshold_name: str | None
    current_threshold: float | None
    proposed_threshold: float | None
    waste_ratio: float
    session_count: int
    action: str
    justification: str


def waste_ratio(metrics: dict[str, int | float]) -> float:
    aligned = int(metrics["aligned_tokens"])
    waste = int(metrics["waste_tokens"])
    total = aligned + waste
    return waste / total if total else 0.0


def propose_thresholds(
    report: dict[str, dict[str, int | float]],
    *,
    local_answer_confidence: float,
    local_code_confidence: float,
    target_waste_ratio: float = 0.25,
    minimum_sessions: int = 5,
    step_size: float = 0.05,
) -> list[CalibrationProposal]:
    routes = {
        "local_fast": ("local_answer_confidence", local_answer_confidence),
        "local_code": ("local_code_confidence", local_code_confidence),
    }
    proposals: list[CalibrationProposal] = []
    for route, (name, current) in routes.items():
        metrics = report.get(route, {})
        count = int(metrics.get("session_count", 0))
        ratio = waste_ratio(metrics) if metrics else 0.0
        if count < minimum_sessions:
            proposals.append(
                CalibrationProposal(
                    route,
                    name,
                    current,
                    current,
                    ratio,
                    count,
                    "insufficient_data",
                    (
                        f"{count} sessions is below the minimum of {minimum_sessions}; "
                        "no change proposed."
                    ),
                )
            )
            continue
        if ratio > target_waste_ratio:
            proposed = round(min(1.0, current + step_size), 2)
            action = "raise"
            reason = "waste exceeds target, so require higher confidence for local routing"
        elif ratio < target_waste_ratio / 2:
            proposed = round(max(0.0, current - step_size), 2)
            action = "lower"
            reason = "waste is well below target, so allow slightly more local routing"
        else:
            proposed = current
            action = "hold"
            reason = "waste is within the acceptable band"
        proposals.append(
            CalibrationProposal(
                route,
                name,
                current,
                proposed,
                ratio,
                count,
                action,
                f"{reason}: {current:.2f} -> {proposed:.2f}, waste={ratio:.1%}, sessions={count}.",
            )
        )
    return proposals


def _iter_log_files(log_directory: Path) -> list[Path]:
    return sorted(log_directory.glob("*.jsonl")) if log_directory.is_dir() else []


def _basic_route_and_tokens(path: Path) -> tuple[str, int] | None:
    """Read route + coarse token estimate from one telemetry file, no TER needed."""
    route: str | None = None
    tokens = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            decision = entry.get("route_decision")
            if isinstance(decision, dict) and isinstance(decision.get("route"), str):
                route = decision["route"]
            usage = entry.get("message", {}).get("usage")
            if isinstance(usage, dict):
                tokens += int(usage.get("input_tokens", 0) or 0)
                tokens += int(usage.get("output_tokens", 0) or 0)
    if route is None:
        return None
    return route, tokens


def basic_value_report(log_directory: Path) -> dict[str, float | int]:
    """Zero-dependency counterpart to :func:`value_report`.

    Reads only the route metadata and coarse token estimates TGV already
    writes into every telemetry record, so it works immediately with no
    optional TER install. Less precise than TER-scored ``value_report``
    (no aligned/waste distinction), but always runnable.
    """
    total = 0
    local = 0
    total_tokens = 0
    local_tokens = 0
    for path in _iter_log_files(log_directory):
        parsed = _basic_route_and_tokens(path)
        if parsed is None:
            continue
        route, tokens = parsed
        total += 1
        total_tokens += tokens
        if route in {"local_fast", "local_code"}:
            local += 1
            local_tokens += tokens
    return {
        "sessions": total,
        "local_share": round(local / total, 4) if total else 0.0,
        "savings_tokens": local_tokens,
        "savings_ratio": round(local_tokens / total_tokens, 4) if total_tokens else 0.0,
    }


def value_report(sessions: list[ScoredSession]) -> dict[str, float | int]:
    """Return CI-stable value evidence from scored telemetry sessions."""
    total = len(sessions)
    local = sum(item.route in {"local_fast", "local_code"} for item in sessions)
    aligned = sum(item.aligned_tokens for item in sessions)
    waste = sum(item.waste_tokens for item in sessions)
    total_tokens = aligned + waste
    return {
        "sessions": total,
        "local_share": round(local / total, 4) if total else 0.0,
        "savings_tokens": max(0, aligned),
        "savings_ratio": round(aligned / total_tokens, 4) if total_tokens else 0.0,
    }
