from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tgv.bench.observed import _outcome_status, _wilson_interval
from tgv.exceptions import LedgerReadError, TGVError
from tgv.models import OutcomeTask, Route
from tgv.outcomes import OutcomeLedger

BIN_COUNT = 10
BIN_EDGES = tuple(index / BIN_COUNT for index in range(BIN_COUNT + 1))
MINIMUM_SCORED_TASKS = 20
DECISION_SOURCES = frozenset({"deterministic", "model", "scoreboard", "safety-override"})


class ConfidenceDiagnosticError(TGVError):
    """Confidence evidence cannot be read safely."""


@dataclass(frozen=True)
class ConfidenceObservation:
    """Internal task-level evidence; individual observations are never emitted."""

    task_id: str
    confidence: float | None
    confidence_status: str
    route: str
    decision_source: str
    selected_model: str
    capability_tier: str
    task_category: str
    outcome_status: str

    @property
    def outcome(self) -> int | None:
        if self.outcome_status == "verified-success":
            return 1
        if self.outcome_status == "verified-failure":
            return 0
        return None

    @property
    def provenance_status(self) -> str:
        if self.route == "invalid-route" or self.decision_source == "invalid-decision-source":
            return "invalid-provenance"
        if self.route == "unavailable" or self.decision_source == "unavailable":
            return "missing-provenance"
        return "attributable"


def _confidence_value(value: object) -> tuple[float | None, str]:
    if value is None:
        return None, "missing-confidence"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "nonnumeric-confidence"
    if isinstance(value, float) and not math.isfinite(value):
        return None, "nonfinite-confidence"
    # Check range before converting arbitrarily large persisted integers to float.
    if not 0 <= value <= 1:
        return None, "out-of-range-confidence"
    return float(value), "valid"


def _provenance(value: object, allowed: set[str] | frozenset[str], invalid: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "unavailable"
    return value if isinstance(value, str) and value in allowed else invalid


def _observation(row: dict[str, Any]) -> ConfidenceObservation:
    confidence, confidence_status = _confidence_value(row["routing_confidence"])
    metadata = json.loads(row["metadata"])
    if isinstance(metadata, dict):
        route = _provenance(
            metadata.get("authoritative_route"), {r.value for r in Route}, "invalid-route"
        )
        source = _provenance(
            metadata.get("routing_decision_source"), DECISION_SOURCES, "invalid-decision-source"
        )
    else:
        route, source = "invalid-route", "invalid-decision-source"
    # Reuse the established task outcome contract. Confidence is validated separately,
    # before Pydantic could coerce a boolean or malformed value into a probability.
    verification = {
        name: None if row[name] is None else bool(row[name])
        for name in (
            "tests_passed",
            "static_checks_passed",
            "human_accepted",
            "rework_required",
            "safety_violation",
        )
    }
    task = OutcomeTask.model_validate(
        {**row, **verification, "routing_confidence": None, "metadata": {}}
    )
    return ConfidenceObservation(
        task_id=task.task_id,
        confidence=confidence,
        confidence_status=confidence_status,
        route=route,
        decision_source=source,
        selected_model=task.selected_model or "unavailable",
        capability_tier=task.selected_capability_tier.value
        if task.selected_capability_tier
        else "unavailable",
        task_category=task.task_category.value,
        outcome_status=_outcome_status(task),
    )


def _load_observations(ledger: OutcomeLedger, repository_root: Path) -> list[ConfidenceObservation]:
    if not ledger.path.exists():
        return []
    try:
        rows = ledger.confidence_records(ledger.repository_id(repository_root))
    except LedgerReadError as exc:
        raise ConfidenceDiagnosticError(f"Cannot read confidence evidence: {exc}") from None
    except sqlite3.Error:
        raise ConfidenceDiagnosticError(
            "Cannot read confidence evidence: malformed outcome schema or unreadable database"
        ) from None
    try:
        observations = [_observation(row) for row in rows]
    except (ValueError, TypeError, KeyError):
        # Pydantic/JSON errors can contain raw persisted values. Never expose those.
        raise ConfidenceDiagnosticError(
            "Cannot read confidence evidence: malformed task fields or routing metadata"
        ) from None
    if len({item.task_id for item in observations}) != len(observations):
        raise ConfidenceDiagnosticError(
            "Cannot read confidence evidence: duplicate outcome task ids"
        )
    return sorted(observations, key=lambda item: item.task_id)


def _reliability_bins(scored: list[tuple[float, int]]) -> list[dict[str, Any]]:
    populations: list[list[tuple[float, int]]] = [[] for _ in range(BIN_COUNT)]
    for confidence, outcome in scored:
        index = min(bisect_right(BIN_EDGES, confidence) - 1, BIN_COUNT - 1)
        populations[index].append((confidence, outcome))
    bins: list[dict[str, Any]] = []
    for index, population in enumerate(populations):
        count = len(population)
        successes = sum(outcome for _, outcome in population)
        mean = math.fsum(confidence for confidence, _ in population) / count if count else None
        rate = successes / count if count else None
        bins.append(
            {
                "lower_bound": BIN_EDGES[index],
                "upper_bound": BIN_EDGES[index + 1],
                "upper_inclusive": index == BIN_COUNT - 1,
                "count": count,
                "mean_confidence": mean,
                "verified_success_rate": rate,
                "wilson_interval_95": _wilson_interval(successes, count),
                "calibration_gap": abs(rate - mean)
                if rate is not None and mean is not None
                else None,
            }
        )
    return bins


def _metrics(observations: list[ConfidenceObservation]) -> dict[str, Any]:
    total = len(observations)
    outcomes = Counter(item.outcome_status for item in observations)
    confidence_counts = Counter(item.confidence_status for item in observations)
    successes, failures = outcomes["verified-success"], outcomes["verified-failure"]
    verified = successes + failures
    scored = [
        (item.confidence, item.outcome)
        for item in observations
        if item.confidence is not None and item.outcome is not None
    ]
    count = len(scored)
    bins = _reliability_bins(scored)
    invalid_reasons = {
        reason: confidence_counts[reason]
        for reason in (
            "missing-confidence",
            "nonnumeric-confidence",
            "nonfinite-confidence",
            "out-of-range-confidence",
        )
    }
    exclusions = {
        **invalid_reasons,
        "unverified-outcome": outcomes["unverified"],
        "incomplete-outcome": outcomes["incomplete"],
    }
    return {
        "total_tasks": total,
        "valid_confidence_tasks": confidence_counts["valid"],
        "missing_confidence_tasks": confidence_counts["missing-confidence"],
        "invalid_confidence_tasks": total
        - confidence_counts["valid"]
        - confidence_counts["missing-confidence"],
        "verified_tasks": verified,
        "verified_successes": successes,
        "verified_failures": failures,
        "unverified_tasks": outcomes["unverified"],
        "incomplete_tasks": outcomes["incomplete"],
        "scored_observations": count,
        "excluded_tasks": total - count,
        "exclusion_reasons": exclusions,
        "provenance_counts": dict(
            sorted(Counter(item.provenance_status for item in observations).items())
        ),
        "verification_coverage": verified / total if total else None,
        "confidence_coverage": confidence_counts["valid"] / total if total else None,
        "scoring_coverage": count / total if total else None,
        "verified_success_rate": successes / verified if verified else None,
        "verified_success_interval_95": _wilson_interval(successes, verified),
        "brier_score": math.fsum((confidence - outcome) ** 2 for confidence, outcome in scored)
        / count
        if count
        else None,
        "expected_calibration_error": math.fsum(
            item["count"] / count * item["calibration_gap"] for item in bins if item["count"]
        )
        if count
        else None,
        "reliability_bins": bins,
        "evidence_sufficiency": {
            "status": "missing"
            if count == 0
            else "limited"
            if count < MINIMUM_SCORED_TASKS
            else "sufficient",
            "sample_size": count,
            "minimum_scored_tasks": MINIMUM_SCORED_TASKS,
            "meaning": "descriptive sample-size warning, not proof of validity or causal benefit",
        },
    }


def _group(observations: list[ConfidenceObservation], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[ConfidenceObservation]] = {}
    for observation in observations:
        groups.setdefault(getattr(observation, field), []).append(observation)
    return {key: _metrics(items) for key, items in sorted(groups.items())}


def _serialize(value: Any) -> Any:
    """Round only at the report boundary, after every statistical calculation."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def confidence_report(ledger: OutcomeLedger, repository_root: Path) -> dict[str, Any]:
    observations = _load_observations(ledger, repository_root)
    report: dict[str, Any] = _serialize(
        {
            "source": "confidence-ledger",
            "repository_id": ledger.repository_id(repository_root),
            "interpretation": (
                "exploratory probability-style diagnostics of persisted routing confidence"
            ),
            "population": (
                "pooled verified tasks with valid confidence; mixed sources and provenance"
            ),
            "overall": _metrics(observations),
            "groups": {
                f"by_{field}": _group(observations, field)
                for field in (
                    "route",
                    "selected_model",
                    "capability_tier",
                    "decision_source",
                    "task_category",
                    "provenance_status",
                )
            },
            "methodology": {
                "observation_unit": (
                    "one unique outcome task; attempts and sessions do not multiply observations"
                ),
                "outcome_contract": (
                    "TGV verified task outcomes; success=1, failure=0; no TER requirement"
                ),
                "brier_score": "sum((confidence - outcome)^2) / scored_observations",
                "expected_calibration_error": (
                    "sum(bin_count / scored_observations * abs(bin_success_rate - "
                    "bin_mean_confidence))"
                ),
                "bin_count": BIN_COUNT,
                "bin_boundaries": (
                    "[lower, upper), except the final bin [0.9, 1.0]; internal boundaries enter "
                    "the higher bin"
                ),
                "confidence_interval": "Wilson score interval at 95%; full precision internally",
                "display_decimal_places": 6,
                "coverage_denominators": {
                    "verification_coverage": "verified_tasks / total_tasks",
                    "confidence_coverage": "valid_confidence_tasks / total_tasks",
                    "scoring_coverage": "scored_observations / total_tasks",
                    "verified_success_rate": (
                        "verified_successes / verified_tasks (including tasks lacking valid "
                        "confidence)"
                    ),
                },
                "exclusion_counts": (
                    "reasons can overlap; excluded_tasks is the unique excluded count"
                ),
                "provenance_policy": (
                    "missing and invalid provenance remain visible and scored when otherwise "
                    "eligible; by_provenance_status separates them"
                ),
                "group_policy": (
                    "each group computes its own scores and bins; model and tier describe final "
                    "selection, not confidence origin"
                ),
            },
            "limitations": [
                "Routing confidence is not an established probability of verified task success.",
                (
                    "Deterministic confidence uses route-specific constants; model confidence is "
                    "provider-reported."
                ),
                (
                    "Scoreboard and safety overrides retain baseline routing confidence even when "
                    "the final route changes."
                ),
                (
                    "Scoreboard candidate confidence and expected success probability are "
                    "separate fields, not the persisted routing confidence."
                ),
                (
                    "Pooled scores mix confidence meanings; compare source and provenance groups "
                    "before interpreting them."
                ),
                "Missing historical provenance is never inferred from capability tiers.",
                (
                    "SQLite REAL affinity can erase original types: historical booleans may "
                    "become 0/1 and NaN may become NULL."
                ),
                (
                    "Verification coverage, missing confidence, selection bias, task difficulty "
                    "and model/policy changes can bias scores."
                ),
                (
                    "Task outcomes follow the existing completion and verification contract; "
                    "safety violations are failures even if incomplete."
                ),
                (
                    "Distinct task ids can still describe dependent or repeated work; Wilson "
                    "intervals do not correct that dependence."
                ),
                "ECE depends on binning and sample size; low ECE does not prove optimal routing.",
                (
                    "Small groups and sparse bins have substantial uncertainty; no causal group "
                    "comparisons are supported."
                ),
                "These diagnostics authorize no routing, ranking or threshold changes.",
            ],
        }
    )

    return report


def render_confidence_report(report: dict[str, Any]) -> str:
    overall = report["overall"]

    def display(value: object) -> str:
        return "unavailable" if value is None else str(value)

    lines = [
        f"source: {report['source']}",
        f"interpretation: {report['interpretation']}",
        f"population: {report['population']}",
        f"tasks: total={overall['total_tasks']} verified={overall['verified_tasks']} "
        f"success={overall['verified_successes']} failure={overall['verified_failures']} "
        f"unverified={overall['unverified_tasks']} incomplete={overall['incomplete_tasks']}",
        f"confidence: valid={overall['valid_confidence_tasks']} "
        f"missing={overall['missing_confidence_tasks']} "
        f"invalid={overall['invalid_confidence_tasks']} scored={overall['scored_observations']}",
        "coverage (denominator: all tasks): "
        f"verification={display(overall['verification_coverage'])} "
        f"confidence={display(overall['confidence_coverage'])}",
        f"Brier={display(overall['brier_score'])} "
        f"ECE={display(overall['expected_calibration_error'])} "
        f"evidence={overall['evidence_sufficiency']['status']}",
        "exclusions (may overlap): " + json.dumps(overall["exclusion_reasons"], sort_keys=True),
        "reliability bins: [lower, upper), final bin includes 1.0",
    ]
    for item in overall["reliability_bins"]:
        bracket = "]" if item["upper_inclusive"] else ")"
        lines.append(
            f"  [{item['lower_bound']:.1f}, {item['upper_bound']:.1f}{bracket} n={item['count']} "
            f"mean={display(item['mean_confidence'])} "
            f"success={display(item['verified_success_rate'])} "
            f"Wilson-95%={display(item['wilson_interval_95'])} "
            f"gap={display(item['calibration_gap'])}"
        )
    for dimension, groups in report["groups"].items():
        for name, metrics in groups.items():
            # Quote model labels to escape terminal control characters.
            lines.append(
                f"{dimension}:{json.dumps(name)} tasks={metrics['total_tasks']} "
                f"verified={metrics['verified_tasks']} scored={metrics['scored_observations']} "
                f"Brier={display(metrics['brier_score'])} "
                f"ECE={display(metrics['expected_calibration_error'])} "
                f"evidence={metrics['evidence_sufficiency']['status']}"
            )
    lines.extend(f"limitation: {item}" for item in report["limitations"])
    return "\n".join(lines)
