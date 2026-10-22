from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from tgv.config import default_config
from tgv.intelligence import TaskProfiler
from tgv.models import OutcomeTask, TaskCategory
from tgv.router import Router

RELEASE_GATE: dict[str, float | int] = {
    "minimum_cases": 100,
    "task_category_accuracy": 0.90,
    "complexity_band_accuracy": 0.90,
    "acceptable_route_accuracy": 0.95,
    "maximum_false_low_risk_rate": 0.0,
}


def default_cases_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "intelligence_cases.jsonl"


def _band(score: float) -> str:
    if score < 35:
        return "low"
    if score < 65:
        return "medium"
    return "high"


def _release_gate(report: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "minimum_cases": report["total_cases"] >= RELEASE_GATE["minimum_cases"],
        "task_category_accuracy": (
            report["task_category_accuracy"] >= RELEASE_GATE["task_category_accuracy"]
        ),
        "complexity_band_accuracy": (
            report["complexity_band_accuracy"] >= RELEASE_GATE["complexity_band_accuracy"]
        ),
        "acceptable_route_accuracy": (
            report["acceptable_route_accuracy"] >= RELEASE_GATE["acceptable_route_accuracy"]
        ),
        "false_low_risk_rate": (
            report["false_low_risk_rate"] <= RELEASE_GATE["maximum_false_low_risk_rate"]
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "thresholds": RELEASE_GATE,
    }


def run_benchmark(path: Path) -> dict[str, Any]:
    config = default_config()
    profiler = TaskProfiler(config.intelligence)
    router = Router(config.routing)
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    case_ids = [str(row.get("case_id", "")) for row in rows]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("benchmark case_id values must be unique")

    category_correct = 0
    band_correct = 0
    route_correct = 0
    false_low_risk = 0
    details: list[dict[str, Any]] = []
    category_distribution: Counter[str] = Counter()
    band_distribution: Counter[str] = Counter()
    source_distribution: Counter[str] = Counter()
    for row in rows:
        expected_category = TaskCategory(row["expected_task_category"])
        expected_band = str(row["expected_complexity_band"])
        acceptable_routes = row["acceptable_routes"]
        if expected_band not in {"low", "medium", "high"}:
            raise ValueError(f"invalid complexity band for {row['case_id']}: {expected_band}")
        if not isinstance(acceptable_routes, list) or not acceptable_routes:
            raise ValueError(f"acceptable_routes must be non-empty for {row['case_id']}")

        category_distribution[expected_category.value] += 1
        band_distribution[expected_band] += 1
        source_distribution[str(row.get("source", "unspecified"))] += 1
        profile = profiler.profile(row["request"])
        decision = router.deterministic(row["request"])
        category_ok = profile.task_category == expected_category
        band_ok = _band(profile.complexity_score) == expected_band
        route_ok = decision.route.value in acceptable_routes
        risk_expected = bool(row.get("expected_risk_factors"))
        risk_missed = (
            risk_expected and profile.security_risk_score < 60 and not decision.risk_signals
        )
        category_correct += int(category_ok)
        band_correct += int(band_ok)
        route_correct += int(route_ok)
        false_low_risk += int(risk_missed)
        details.append(
            {
                "case_id": row["case_id"],
                "category_correct": category_ok,
                "complexity_band_correct": band_ok,
                "route_acceptable": route_ok,
                "false_low_risk": risk_missed,
            }
        )
    total = len(rows)
    report: dict[str, Any] = {
        "benchmark_version": "0.32",
        "total_cases": total,
        "task_category_accuracy": category_correct / total if total else 0.0,
        "complexity_band_accuracy": band_correct / total if total else 0.0,
        "acceptable_route_accuracy": route_correct / total if total else 0.0,
        "false_low_risk_rate": false_low_risk / total if total else 0.0,
        "distribution": {
            "task_category": dict(sorted(category_distribution.items())),
            "complexity_band": dict(sorted(band_distribution.items())),
            "source": dict(sorted(source_distribution.items())),
        },
        "details": details,
    }
    report["release_gate"] = _release_gate(report)
    report["summary"] = (
        f"cases={total} category={report['task_category_accuracy']:.1%} "
        f"complexity={report['complexity_band_accuracy']:.1%} "
        f"routes={report['acceptable_route_accuracy']:.1%} "
        f"false-low-risk={report['false_low_risk_rate']:.1%} "
        f"gate={'PASS' if report['release_gate']['passed'] else 'FAIL'}"
    )
    return report


OUTCOME_RELEASE_GATE: dict[str, float | int] = {
    "minimum_verified_samples": 20,
    "minimum_verified_success_rate": 0.85,
    "maximum_safety_violation_rate": 0.0,
}


def _outcome_success(record: OutcomeTask) -> bool | None:
    if record.safety_violation:
        return False
    if record.execution_status not in {"completed", "succeeded"}:
        return None
    observed = any(
        value is not None
        for value in (
            record.tests_passed,
            record.static_checks_passed,
            record.human_accepted,
            record.rework_required,
        )
    )
    if not observed:
        return None
    if record.tests_passed is False or record.static_checks_passed is False:
        return False
    return not (record.human_accepted is False or record.rework_required is True)


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _outcome_group(records: list[OutcomeTask]) -> dict[str, Any]:
    verified = [value for record in records if (value := _outcome_success(record)) is not None]
    accepted = [record.human_accepted for record in records if record.human_accepted is not None]
    rework = [record.rework_required for record in records if record.rework_required is not None]
    safety = [record.safety_violation for record in records if record.safety_violation is not None]
    latencies = [record.latency_seconds for record in records if record.latency_seconds is not None]
    costs = [record.estimated_cost for record in records if record.estimated_cost is not None]
    return {
        "total": len(records),
        "verified_samples": len(verified),
        "verified_success_rate": round(sum(bool(value) for value in verified) / len(verified), 4)
        if verified
        else None,
        "human_feedback_samples": len(accepted),
        "acceptance_rate": round(sum(bool(value) for value in accepted) / len(accepted), 4)
        if accepted
        else None,
        "rework_samples": len(rework),
        "rework_rate": round(sum(bool(value) for value in rework) / len(rework), 4)
        if rework
        else None,
        "safety_observations": len(safety),
        "safety_violation_rate": round(sum(bool(value) for value in safety) / len(safety), 4)
        if safety
        else 0.0,
        "retry_rate": round(sum(record.retry_count > 0 for record in records) / len(records), 4)
        if records
        else 0.0,
        "average_latency_seconds": _average([float(value) for value in latencies]),
        "average_estimated_cost": _average([float(value) for value in costs]),
    }


def run_outcome_benchmark(
    records: list[OutcomeTask],
    *,
    minimum_verified_samples: int = int(OUTCOME_RELEASE_GATE["minimum_verified_samples"]),
    minimum_verified_success_rate: float = float(
        OUTCOME_RELEASE_GATE["minimum_verified_success_rate"]
    ),
    maximum_safety_violation_rate: float = float(
        OUTCOME_RELEASE_GATE["maximum_safety_violation_rate"]
    ),
) -> dict[str, Any]:
    """Evaluate privacy-minimal observed outcomes from a real TGV outcome ledger."""
    if minimum_verified_samples < 1:
        raise ValueError("minimum_verified_samples must be at least 1")
    if not 0 <= minimum_verified_success_rate <= 1:
        raise ValueError("minimum_verified_success_rate must be between 0 and 1")
    if not 0 <= maximum_safety_violation_rate <= 1:
        raise ValueError("maximum_safety_violation_rate must be between 0 and 1")

    overall = _outcome_group(records)
    by_model: dict[str, list[OutcomeTask]] = {}
    by_tier: dict[str, list[OutcomeTask]] = {}
    by_category: dict[str, list[OutcomeTask]] = {}
    for record in records:
        by_model.setdefault(record.selected_model or "unassigned", []).append(record)
        tier = (
            record.selected_capability_tier.value
            if record.selected_capability_tier
            else "unassigned"
        )
        by_tier.setdefault(tier, []).append(record)
        by_category.setdefault(record.task_category.value, []).append(record)

    verified_success_rate = overall["verified_success_rate"]
    safety_violation_rate = float(overall["safety_violation_rate"])
    checks = {
        "minimum_verified_samples": overall["verified_samples"] >= minimum_verified_samples,
        "minimum_verified_success_rate": (
            verified_success_rate is not None
            and float(verified_success_rate) >= minimum_verified_success_rate
        ),
        "maximum_safety_violation_rate": safety_violation_rate <= maximum_safety_violation_rate,
    }
    report: dict[str, Any] = {
        "benchmark_version": "0.32",
        "source": "outcome-ledger",
        "overall": overall,
        "by_model": {key: _outcome_group(value) for key, value in sorted(by_model.items())},
        "by_capability_tier": {
            key: _outcome_group(value) for key, value in sorted(by_tier.items())
        },
        "by_task_category": {
            key: _outcome_group(value) for key, value in sorted(by_category.items())
        },
        "release_gate": {
            "passed": all(checks.values()),
            "checks": checks,
            "thresholds": {
                "minimum_verified_samples": minimum_verified_samples,
                "minimum_verified_success_rate": minimum_verified_success_rate,
                "maximum_safety_violation_rate": maximum_safety_violation_rate,
            },
        },
    }
    report["summary"] = (
        f"outcomes={overall['total']} verified={overall['verified_samples']} "
        f"success={verified_success_rate if verified_success_rate is not None else 'n/a'} "
        f"safety={safety_violation_rate:.1%} "
        f"gate={'PASS' if report['release_gate']['passed'] else 'INSUFFICIENT/FAIL'}"
    )
    return report
