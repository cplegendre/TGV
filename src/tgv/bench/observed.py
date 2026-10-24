from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tgv.evaluation import _outcome_success
from tgv.exceptions import TGVError
from tgv.linked_efficiency import (
    _aggregate,
    _canonical_entries,
    _LinkedLedger,
    _load_linked_ledger,
    _measurement_view,
    _MeasurementView,
    _route_for_task,
)
from tgv.models import ExecutionAttempt, OutcomeTask
from tgv.outcomes import LedgerDiagnostics, OutcomeLedger


class ObservedBenchmarkError(TGVError):
    """Raised when persisted observational evidence cannot be correlated safely."""


@dataclass(frozen=True)
class SessionObservation:
    """One linked session and its selected persisted measurement."""

    task: OutcomeTask
    attempt: ExecutionAttempt
    session_link_id: str
    agent: str
    measurement: _MeasurementView | None
    raw_measurement_count: int
    invalid_measurement_count: int

    @property
    def measurement_status(self) -> str:
        return "measured" if self.measurement is not None else "missing"


@dataclass(frozen=True)
class TaskObservation:
    """Task-level outcome evidence with all related attempts and sessions."""

    task: OutcomeTask
    attempts: list[ExecutionAttempt]
    sessions: list[SessionObservation]
    outcome_status: str


@dataclass(frozen=True)
class _ObservationData:
    linked: _LinkedLedger
    tasks: list[TaskObservation]
    unattributed_invalid_measurements: int
    selected_analyzer_scope: tuple[str, str] | None
    diagnostics: LedgerDiagnostics


def _outcome_status(task: OutcomeTask) -> str:
    value = _outcome_success(task)
    if value is True:
        return "verified-success"
    if value is False:
        return "verified-failure"
    if task.execution_status not in {"completed", "succeeded"}:
        return "incomplete"
    return "unverified"


def _load_observations(ledger: OutcomeLedger, repository_root: Path) -> _ObservationData:
    with ledger.evidence_read() as diagnostics:
        return _load_observations_with(ledger, repository_root, diagnostics)


def _load_observations_with(
    ledger: OutcomeLedger,
    repository_root: Path,
    diagnostics: LedgerDiagnostics,
) -> _ObservationData:
    linked = _load_linked_ledger(ledger, repository_root)
    links_by_id = {
        link.session_link_id: (task, attempt, link) for task, attempt, link in linked.links
    }
    tasks_by_id = {task.task_id: task for task in linked.tasks}
    raw_counts: Counter[str] = Counter()
    invalid_counts: Counter[str] = Counter()
    valid_entries: list[_MeasurementView] = []
    unattributed_invalid_measurements = 0

    for attempt in linked.attempts:
        task = tasks_by_id[attempt.task_id]
        for measurement in ledger.ter_measurements(attempt.attempt_id):
            if measurement.attempt_id != attempt.attempt_id:
                raise ObservedBenchmarkError(
                    f"Measurement {measurement.measurement_id} has an invalid attempt association"
                )
            if measurement.session_link_id is None:
                unattributed_invalid_measurements += 1
                continue
            association = links_by_id.get(measurement.session_link_id)
            if association is None:
                if measurement.session_link_id in diagnostics.quarantined_session_link_ids:
                    diagnostics.excluded_measurements += 1
                    continue
                raise ObservedBenchmarkError(
                    f"Measurement {measurement.measurement_id} has an invalid session association"
                )
            measurement_task, measurement_attempt, link = association
            if (
                measurement_task.task_id != task.task_id
                or measurement_attempt.attempt_id != attempt.attempt_id
                or link.attempt_id != attempt.attempt_id
            ):
                raise ObservedBenchmarkError(
                    f"Measurement {measurement.measurement_id} has an invalid outcome association"
                )
            raw_counts[link.session_link_id] += 1
            if (
                not measurement.analyzer.strip()
                or not measurement.analyzer_version.strip()
                or (view := _measurement_view(task, attempt, link, measurement)) is None
            ):
                invalid_counts[link.session_link_id] += 1
                continue
            valid_entries.append(view)

    canonical_entries = _canonical_entries(valid_entries)
    canonical_by_link = {entry.link.session_link_id: entry for entry in canonical_entries}
    sessions_by_task: dict[str, list[SessionObservation]] = {
        task.task_id: [] for task in linked.tasks
    }
    for task, attempt, link in linked.links:
        sessions_by_task[task.task_id].append(
            SessionObservation(
                task=task,
                attempt=attempt,
                session_link_id=link.session_link_id,
                agent=link.agent,
                measurement=canonical_by_link.get(link.session_link_id),
                raw_measurement_count=raw_counts[link.session_link_id],
                invalid_measurement_count=invalid_counts[link.session_link_id],
            )
        )

    attempts_by_task: dict[str, list[ExecutionAttempt]] = {
        task.task_id: [] for task in linked.tasks
    }
    for attempt in linked.attempts:
        attempts_by_task[attempt.task_id].append(attempt)

    tasks = [
        TaskObservation(
            task=task,
            attempts=attempts_by_task[task.task_id],
            sessions=sessions_by_task[task.task_id],
            outcome_status=_outcome_status(task),
        )
        for task in linked.tasks
    ]
    return _ObservationData(
        linked=linked,
        tasks=tasks,
        unattributed_invalid_measurements=unattributed_invalid_measurements,
        selected_analyzer_scope=_select_analyzer_scope(canonical_entries),
        diagnostics=diagnostics,
    )


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z * (proportion * (1 - proportion) / total + z * z / (4 * total * total)) ** 0.5
    ) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _rounded_wilson_interval(successes: int, total: int) -> list[float] | None:
    interval = _wilson_interval(successes, total)
    return [round(bound, 4) for bound in interval] if interval is not None else None


def _token_metrics(entries: list[_MeasurementView]) -> dict[str, Any]:
    if not entries:
        return {
            "aggregate_ter": None,
            "aligned_tokens": 0,
            "waste_tokens": 0,
            "total_tokens": 0,
        }
    aggregate = _aggregate(entries)
    return {
        "aggregate_ter": aggregate["aggregate_ter"],
        "aligned_tokens": aggregate["aligned_tokens"],
        "waste_tokens": aggregate["waste_tokens"],
        "total_tokens": aggregate["total_tokens"],
    }


def _analyzer_scope_entries(
    entries: list[_MeasurementView],
) -> dict[tuple[str, str], list[_MeasurementView]]:
    if not entries:
        return {}
    scopes: dict[tuple[str, str], list[_MeasurementView]] = {}
    for entry in entries:
        key = (entry.measurement.analyzer, entry.measurement.analyzer_version)
        scopes.setdefault(key, []).append(entry)
    return scopes


def _select_analyzer_scope(
    entries: list[_MeasurementView],
) -> tuple[str, str] | None:
    scopes = _analyzer_scope_entries(entries)
    if not scopes:
        return None
    return sorted(
        scopes,
        key=lambda key: (-len(scopes[key]), key),
    )[0]


def _analyzer_scope_summaries(
    scopes: dict[tuple[str, str], list[_MeasurementView]],
    selected_scope: tuple[str, str] | None,
) -> list[dict[str, Any]]:
    return [
        {
            "analyzer": analyzer,
            "analyzer_version": version,
            "selected": (analyzer, version) == selected_scope,
            **_token_metrics(items),
            "measured_session_count": len(items),
        }
        for (analyzer, version), items in sorted(scopes.items())
    ]


def _evidence_sufficiency(
    aggregated_sessions: int, verified_outcomes: int
) -> dict[str, Any]:
    minimum_sample_size = 20
    reasons: list[str] = []
    if aggregated_sessions == 0:
        reasons.append("missing comparable TER measurements")
    if verified_outcomes == 0:
        reasons.append("missing verified outcomes")
    if not reasons and (
        aggregated_sessions < minimum_sample_size or verified_outcomes < minimum_sample_size
    ):
        reasons.append("sample size is below the conservative minimum")
    status = (
        "missing" if aggregated_sessions == 0 or verified_outcomes == 0 else "limited"
        if aggregated_sessions < minimum_sample_size or verified_outcomes < minimum_sample_size
        else "sufficient"
    )
    return {
        "status": status,
        "minimum_sample_size": minimum_sample_size,
        "aggregated_session_sample_size": aggregated_sessions,
        "verified_outcome_sample_size": verified_outcomes,
        "reasons": reasons,
    }


def _group_metrics(
    tasks: list[TaskObservation], selected_scope: tuple[str, str] | None
) -> dict[str, Any]:
    sessions = [session for task in tasks for session in task.sessions]
    entries = [session.measurement for session in sessions if session.measurement is not None]
    scopes = _analyzer_scope_entries(entries)
    selected_entries = scopes.get(selected_scope, []) if selected_scope is not None else []
    analyzer_scopes = _analyzer_scope_summaries(scopes, selected_scope)
    outcome_counts = Counter(task.outcome_status for task in tasks)
    verified_success = outcome_counts["verified-success"]
    verified_failure = outcome_counts["verified-failure"]
    verified_outcomes = verified_success + verified_failure
    linked_tasks = sum(bool(task.sessions) for task in tasks)
    measured_sessions = len(entries)
    aggregated_sessions = len(selected_entries)
    linked_sessions = len(sessions)
    selected_analyzer, selected_version = (
        selected_scope if selected_scope is not None else (None, None)
    )
    return {
        "status": (
            "empty"
            if not tasks
            else "insufficient-data"
            if measured_sessions == 0 or verified_outcomes == 0
            else "observational"
        ),
        "observation_count": linked_sessions,
        "linked_task_count": linked_tasks,
        "eligible_task_count": len(tasks),
        "attempt_count": sum(len(task.attempts) for task in tasks),
        "attempt_status_counts": dict(
            sorted(Counter(attempt.status for task in tasks for attempt in task.attempts).items())
        ),
        "linked_session_count": linked_sessions,
        "measured_session_count": measured_sessions,
        "aggregated_session_count": aggregated_sessions,
        "excluded_from_aggregate_sessions": measured_sessions - aggregated_sessions,
        "missing_measurement_sessions": linked_sessions - measured_sessions,
        "invalid_measurement_count": sum(session.invalid_measurement_count for session in sessions),
        "sessions_with_invalid_measurements": sum(
            session.invalid_measurement_count > 0 for session in sessions
        ),
        "measurement_coverage": (
            round(measured_sessions / linked_sessions, 4) if linked_sessions else None
        ),
        "verified_outcome_count": verified_outcomes,
        "verified_outcome_coverage": (round(verified_outcomes / len(tasks), 4) if tasks else None),
        "verified_success_count": verified_success,
        "verified_failure_count": verified_failure,
        "unverified_outcome_count": outcome_counts["unverified"],
        "incomplete_task_count": outcome_counts["incomplete"],
        "verified_success_rate": (
            round(verified_success / verified_outcomes, 4) if verified_outcomes else None
        ),
        "verified_success_confidence_interval_95": _rounded_wilson_interval(
            verified_success, verified_outcomes
        ),
        "selected_analyzer": selected_analyzer,
        "selected_analyzer_version": selected_version,
        "mixed_analyzer_versions": len(analyzer_scopes) > 1,
        "analyzer_scopes": analyzer_scopes,
        "evidence_sufficiency": _evidence_sufficiency(aggregated_sessions, verified_outcomes),
        **_token_metrics(selected_entries),
    }


def _group(
    tasks: list[TaskObservation],
    key: Callable[[TaskObservation], str],
    selected_scope: tuple[str, str] | None,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TaskObservation]] = {}
    for task in tasks:
        grouped.setdefault(key(task), []).append(task)
    return {
        name: _group_metrics(items, selected_scope) for name, items in sorted(grouped.items())
    }


def _serialize_session(
    session: SessionObservation, selected_analyzer_scope: tuple[str, str] | None
) -> dict[str, Any]:
    measurement = session.measurement
    return {
        "task_id": session.task.task_id,
        "attempt_id": session.attempt.attempt_id,
        "attempt_status": session.attempt.status,
        "session_link_id": session.session_link_id,
        "agent": session.agent,
        "routing_decision_id": session.attempt.routing_decision_id,
        "route": _route_for_task(session.task),
        "selected_model": session.task.selected_model or "unassigned",
        "selected_capability_tier": (
            session.task.selected_capability_tier.value
            if session.task.selected_capability_tier
            else "unassigned"
        ),
        "task_category": session.task.task_category.value,
        "outcome_status": _outcome_status(session.task),
        "measurement_status": session.measurement_status,
        "aggregated_in_report": (
            session.measurement is not None
            and (
                session.measurement.measurement.analyzer,
                session.measurement.measurement.analyzer_version,
            )
            == selected_analyzer_scope
        ),
        "measurement_id": (measurement.measurement.measurement_id if measurement else None),
        "analyzer": measurement.measurement.analyzer if measurement else None,
        "analyzer_version": (measurement.measurement.analyzer_version if measurement else None),
        "measured_at": (measurement.measurement.measured_at.isoformat() if measurement else None),
        "metrics": (
            {
                "aggregate_ter": measurement.aggregate_ter,
                "aligned_tokens": measurement.aligned_tokens,
                "waste_tokens": measurement.waste_tokens,
                "total_tokens": measurement.total_tokens,
            }
            if measurement
            else None
        ),
    }


def _empty_benchmark(repository_id: str) -> dict[str, Any]:
    overall = _group_metrics([], None)
    return {
        "source": "observed-ledger",
        "repository_id": repository_id,
        "status": overall["status"],
        "coverage": overall,
        "overall": overall,
        "groups": {
            "by_route": _group([], lambda task: _route_for_task(task.task), None),
            "by_model": _group([], lambda task: task.task.selected_model or "unassigned", None),
            "by_capability_tier": _group(
                [],
                lambda task: (
                    task.task.selected_capability_tier.value
                    if task.task.selected_capability_tier
                    else "unassigned"
                ),
                None,
            ),
            "by_task_category": _group(
                [], lambda task: task.task.task_category.value, None
            ),
        },
        "observations": [],
        "methodology": {
            "design": "observational",
            "measurement_source": "persisted TER measurements",
            "measurement_unit": "linked agent session",
            "task_outcome_unit": "unique outcome task",
            "measurement_selection": (
                "most recent valid measurement per session link; ties break by measurement id"
            ),
            "analyzer_policy": (
                "select the analyzer with the most canonical measured sessions globally; "
                "ties break lexicographically; apply that one scope to every group; "
                "all scopes are reported separately"
            ),
            "ter_aggregation": (
                "sum(aggregate_ter * total_tokens) / sum(total_tokens); "
                "a measured zero-token group uses TER 1.0"
            ),
            "verified_success_denominator": "verified-success plus verified-failure tasks",
            "multiple_attempt_policy": (
                "attempts and sessions remain traceable; task outcomes count once"
            ),
            "confidence_interval": "Wilson score interval at 95%",
            "insufficient_data_policy": (
                "report evidence separately from methodology; evidence is missing when "
                "either measurements or verified outcomes are absent, limited below 20 "
                "of each, and sufficient at 20 or more of each"
            ),
            "attempt_statistics": "attempt statuses are attempt-level diagnostics",
        },
        "limitations": [
            "Selection bias may affect which tasks, sessions, and routes have measurements.",
            "Missing measurements and unequal verification coverage can bias observed rates.",
            "Task difficulty and model-selection confounding are not controlled.",
            "Small samples produce wide confidence intervals.",
            "Different analyzer versions may not be directly comparable.",
            (
                "Repeated attempts can affect session-level efficiency but not "
                "task-level outcome counts."
            ),
            "No counterfactual execution exists, so alternate routing performance is unknown.",
            "TER efficiency is not verified correctness.",
        ],
    }


def observed_benchmark(ledger: OutcomeLedger, repository_root: Path) -> dict[str, Any]:
    """Report observational evidence already persisted in the outcome ledger."""
    if not ledger.path.is_file():
        return _empty_benchmark(ledger.repository_id(repository_root))
    read_only_ledger = (
        ledger if ledger.read_only else OutcomeLedger(ledger.path, read_only=True)
    )
    data = _load_observations(read_only_ledger, repository_root)
    selected_scope = data.selected_analyzer_scope
    overall = _group_metrics(data.tasks, selected_scope)
    overall["invalid_measurement_count"] += data.unattributed_invalid_measurements
    overall["unattributed_invalid_measurements"] = data.unattributed_invalid_measurements
    overall["invalid_records"] = data.diagnostics.to_dict()
    observations = [
        _serialize_session(session, selected_scope)
        for task in data.tasks
        for session in task.sessions
    ]
    observations.sort(
        key=lambda item: (
            item["task_id"],
            item["attempt_id"],
            item["session_link_id"],
        )
    )
    return {
        "source": "observed-ledger",
        "repository_id": ledger.repository_id(repository_root),
        "status": overall["status"],
        "coverage": overall,
        "overall": overall,
        "groups": {
            "by_route": _group(
                data.tasks, lambda task: _route_for_task(task.task), selected_scope
            ),
            "by_model": _group(
                data.tasks,
                lambda task: task.task.selected_model or "unassigned",
                selected_scope,
            ),
            "by_capability_tier": _group(
                data.tasks,
                lambda task: (
                    task.task.selected_capability_tier.value
                    if task.task.selected_capability_tier
                    else "unassigned"
                ),
                selected_scope,
            ),
            "by_task_category": _group(
                data.tasks,
                lambda task: task.task.task_category.value,
                selected_scope,
            ),
        },
        "observations": observations,
        "methodology": {
            "design": "observational",
            "measurement_source": "persisted TER measurements",
            "measurement_unit": "linked agent session",
            "task_outcome_unit": "unique outcome task",
            "measurement_selection": (
                "most recent valid measurement per session link; ties break by measurement id"
            ),
            "analyzer_policy": (
                "select the analyzer with the most canonical measured sessions globally; "
                "ties break lexicographically; apply that one scope to every group; "
                "all scopes are reported separately"
            ),
            "ter_aggregation": (
                "sum(aggregate_ter * total_tokens) / sum(total_tokens); "
                "a measured zero-token group uses TER 1.0"
            ),
            "verified_success_denominator": ("verified-success plus verified-failure tasks"),
            "multiple_attempt_policy": (
                "attempts and sessions remain traceable; task outcomes count once"
            ),
            "confidence_interval": "Wilson score interval at 95%",
            "insufficient_data_policy": (
                "report evidence separately from methodology; evidence is missing when "
                "either measurements or verified outcomes are absent, limited below 20 "
                "of each, and sufficient at 20 or more of each"
            ),
            "attempt_statistics": "attempt statuses are attempt-level diagnostics",
        },
        "limitations": [
            "Selection bias may affect which tasks, sessions, and routes have measurements.",
            "Missing measurements and unequal verification coverage can bias observed rates.",
            "Task difficulty and model-selection confounding are not controlled.",
            "Small samples produce wide confidence intervals.",
            "Different analyzer versions may not be directly comparable.",
            (
                "Repeated attempts can affect session-level efficiency but not "
                "task-level outcome counts."
            ),
            "No counterfactual execution exists, so alternate routing performance is unknown.",
            "TER efficiency is not verified correctness.",
        ],
    }


def _format_rate(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.1%}"


def render_observed_benchmark(report: dict[str, Any]) -> str:
    overall = report["overall"]
    lines = [
        f"source: {report['source']}",
        f"status: {overall['status']}",
        f"tasks: eligible={overall['eligible_task_count']} linked={overall['linked_task_count']}",
        f"attempts: {overall['attempt_count']}",
        f"sessions: linked={overall['linked_session_count']} "
        f"measured={overall['measured_session_count']} "
        f"missing={overall['missing_measurement_sessions']}",
        f"measurement coverage: {_format_rate(overall['measurement_coverage'])}",
        f"outcomes: verified={overall['verified_outcome_count']} "
        f"success={overall['verified_success_count']} "
        f"failure={overall['verified_failure_count']} "
        f"unverified={overall['unverified_outcome_count']} "
        f"incomplete={overall['incomplete_task_count']}",
        f"verified-success rate: {_format_rate(overall['verified_success_rate'])}",
        "group       tasks  sessions  TER     tokens",
    ]
    for dimension, groups in report["groups"].items():
        for group, metrics in groups.items():
            ter = metrics["aggregate_ter"]
            ter_text = "n/a" if ter is None else f"{ter:.4f}"
            lines.append(
                f"{dimension}:{group:<16} {metrics['eligible_task_count']:>5}  "
                f"{metrics['measured_session_count']:>8}  {ter_text:>6}  "
                f"{metrics['total_tokens']:>5}"
            )
    return "\n".join(lines)
