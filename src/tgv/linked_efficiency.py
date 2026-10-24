from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tgv.efficiency import TERAnalysis, TERAnalysisError, ter_analyzer, validate_ter_analysis
from tgv.evaluation import _outcome_success
from tgv.exceptions import TGVError, ValidationFailure
from tgv.models import (
    AgentSessionLink,
    ExecutionAttempt,
    OutcomeTask,
    Route,
    TerMeasurement,
)
from tgv.outcomes import LedgerDiagnostics, OutcomeLedger, TerMeasurementRequest
from tgv.session_linking import ValidatedSession, validate_agent_session


class LinkedEfficiencyError(TGVError):
    """Raised when linked-session measurement data cannot be trusted."""


@dataclass(frozen=True)
class LinkedMeasurement:
    """A persisted TER measurement and its TGV correlation metadata."""

    task: OutcomeTask
    attempt: ExecutionAttempt
    link: AgentSessionLink
    measurement: TerMeasurement


@dataclass(frozen=True)
class _LinkedLedger:
    tasks: list[OutcomeTask]
    attempts: list[ExecutionAttempt]
    links: list[tuple[OutcomeTask, ExecutionAttempt, AgentSessionLink]]


@dataclass(frozen=True)
class _MeasurementView:
    task: OutcomeTask
    attempt: ExecutionAttempt
    link: AgentSessionLink
    measurement: TerMeasurement
    aggregate_ter: float
    aligned_tokens: int
    waste_tokens: int
    total_tokens: int


def _load_linked_ledger(ledger: OutcomeLedger, repository_root: Path) -> _LinkedLedger:
    tasks = sorted(ledger.query(limit=1_000_000), key=lambda task: task.task_id)
    tasks_by_id = {task.task_id: task for task in tasks}
    expected_repository_id = ledger.repository_id(repository_root)
    links: list[tuple[OutcomeTask, ExecutionAttempt, AgentSessionLink]] = []
    diagnostics = ledger.diagnostics
    quarantined_tasks = diagnostics.quarantined_task_ids if diagnostics is not None else set()

    for task in tasks:
        if task.repository_id != expected_repository_id:
            raise LinkedEfficiencyError(
                f"Outcome task {task.task_id} belongs to a different repository"
            )

    attempts: list[ExecutionAttempt] = []
    for attempt in ledger.all_execution_attempts():
        if attempt.task_id in quarantined_tasks:
            if diagnostics is not None:
                diagnostics.excluded_attempts += 1
            continue
        attempt_task = tasks_by_id.get(attempt.task_id)
        if attempt_task is None:
            raise LinkedEfficiencyError(
                f"Execution attempt {attempt.attempt_id} has no outcome task"
            )
        if attempt.repository_id != attempt_task.repository_id:
            raise LinkedEfficiencyError(
                f"Execution attempt {attempt.attempt_id} does not match its outcome task"
            )
        attempts.append(attempt)
        for link in ledger.agent_sessions(attempt.attempt_id):
            if link.attempt_id != attempt.attempt_id:
                raise LinkedEfficiencyError(
                    f"Session link {link.session_link_id} does not match its execution attempt"
                )
            links.append((attempt_task, attempt, link))

    return _LinkedLedger(tasks=tasks, attempts=attempts, links=links)


def _validate_linked_session(
    task: OutcomeTask, attempt: ExecutionAttempt, link: AgentSessionLink
) -> ValidatedSession:
    if link.session_path is None:
        raise LinkedEfficiencyError(
            f"Session link {link.session_link_id} has no explicit session path"
        )
    if link.content_hash is None:
        raise LinkedEfficiencyError(
            f"Session link {link.session_link_id} has no stored content fingerprint"
        )
    path = Path(link.session_path)
    try:
        validated = validate_agent_session(path, link.agent)
    except ValidationFailure as exc:
        raise LinkedEfficiencyError(str(exc)) from exc
    if validated.external_session_id != link.external_session_id:
        raise LinkedEfficiencyError(
            f"Session identity does not match session link {link.session_link_id}"
        )
    if validated.content_hash != link.content_hash:
        raise LinkedEfficiencyError(
            f"Session content changed after session link {link.session_link_id} was recorded"
        )
    return validated


@contextmanager
def _analysis_snapshot(validated: ValidatedSession) -> Iterator[Path]:
    snapshot_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="tgv-ter-", suffix=".jsonl", delete=False
        ) as snapshot:
            snapshot_path = Path(snapshot.name)
            snapshot.write(validated.content)
        os.chmod(snapshot_path, 0o400)
        yield snapshot_path
    finally:
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)


def _validate_analysis(analysis: TERAnalysis, link: AgentSessionLink) -> None:
    validate_ter_analysis(analysis)
    if analysis.aligned_tokens + analysis.waste_tokens == 0:
        raise LinkedEfficiencyError("TER analysis has insufficient token evidence (zero tokens)")
    if analysis.session_id != link.external_session_id:
        raise LinkedEfficiencyError(
            f"TER session identity does not match session link {link.session_link_id}"
        )


def measure_linked_sessions(
    ledger: OutcomeLedger,
    repository_root: Path,
    *,
    analyzer: Callable[[Path], TERAnalysis] | None = None,
) -> list[LinkedMeasurement]:
    """Validate and score every linked real agent session, then persist aggregates."""
    linked_ledger = _load_linked_ledger(ledger, repository_root)
    analyses: list[tuple[OutcomeTask, ExecutionAttempt, AgentSessionLink, TERAnalysis]] = []
    resolved_analyzer = analyzer
    for task, attempt, link in linked_ledger.links:
        validated = _validate_linked_session(task, attempt, link)
        if link.agent == "codex" and analyzer is None:
            raise LinkedEfficiencyError(
                "Codex scoring requires a TER release with a validated Codex loader; "
                "the bundled TER 3.0.0 analyzer does not support Codex"
            )
        with _analysis_snapshot(validated) as snapshot_path:
            if resolved_analyzer is None:
                resolved_analyzer = ter_analyzer()
            analysis = resolved_analyzer(snapshot_path)
            _validate_analysis(analysis, link)
            analyses.append((task, attempt, link, analysis))

    measurements = ledger.record_ter_measurements(
        TerMeasurementRequest(
            attempt_id=attempt.attempt_id,
            analyzer=analysis.analyzer,
            analyzer_version=analysis.analyzer_version,
            metrics={
                "aggregate_ter": analysis.aggregate_ter,
                "aligned_tokens": analysis.aligned_tokens,
                "waste_tokens": analysis.waste_tokens,
                "total_tokens": analysis.total_tokens,
            },
            session_link_id=link.session_link_id,
        )
        for task, attempt, link, analysis in analyses
    )
    return [
        LinkedMeasurement(
            task=task,
            attempt=attempt,
            link=link,
            measurement=measurement,
        )
        for (task, attempt, link, _analysis), measurement in zip(
            analyses, measurements, strict=True
        )
    ]


def _canonical_entries(entries: list[_MeasurementView]) -> list[_MeasurementView]:
    selected: dict[str, _MeasurementView] = {}
    for entry in entries:
        current = selected.get(entry.link.session_link_id)
        if current is None or (
            entry.measurement.measured_at,
            entry.measurement.measurement_id,
        ) > (
            current.measurement.measured_at,
            current.measurement.measurement_id,
        ):
            selected[entry.link.session_link_id] = entry
    return list(selected.values())


def _route_for_task(task: OutcomeTask) -> str:
    route = task.metadata.get("authoritative_route")
    if isinstance(route, str):
        try:
            return Route(route).value
        except ValueError:
            return "invalid-route"
    return "unavailable"


def _outcome_status(task: OutcomeTask) -> str:
    value = _outcome_success(task)
    if value is True:
        return "verified-success"
    if value is False:
        return "verified-failure"
    return "unverified"


def _required_number(metrics: dict[str, float | int | None], name: str) -> float | None:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _required_count(metrics: dict[str, float | int | None], name: str) -> int | None:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _measurement_view(
    task: OutcomeTask,
    attempt: ExecutionAttempt,
    link: AgentSessionLink,
    measurement: TerMeasurement,
) -> _MeasurementView | None:
    aggregate_ter = _required_number(measurement.metrics, "aggregate_ter")
    aligned_tokens = _required_count(measurement.metrics, "aligned_tokens")
    waste_tokens = _required_count(measurement.metrics, "waste_tokens")
    total_tokens = _required_count(measurement.metrics, "total_tokens")
    if aggregate_ter is None or aligned_tokens is None or waste_tokens is None:
        return None
    if "total_tokens" in measurement.metrics and total_tokens is None:
        return None
    try:
        validate_ter_analysis(
            TERAnalysis(
                session_id=link.external_session_id,
                aggregate_ter=aggregate_ter,
                aligned_tokens=aligned_tokens,
                waste_tokens=waste_tokens,
                total_tokens=total_tokens,
                analyzer=measurement.analyzer,
                analyzer_version=measurement.analyzer_version,
            )
        )
    except TERAnalysisError:
        return None
    resolved_total = total_tokens if total_tokens is not None else aligned_tokens + waste_tokens
    return _MeasurementView(
        task=task,
        attempt=attempt,
        link=link,
        measurement=measurement,
        aggregate_ter=aggregate_ter,
        aligned_tokens=aligned_tokens,
        waste_tokens=waste_tokens,
        total_tokens=resolved_total,
    )


def _aggregate(entries: list[_MeasurementView]) -> dict[str, int | float | str | None]:
    total_tokens = sum(entry.total_tokens for entry in entries)
    weighted_ter = (
        sum(entry.aggregate_ter * entry.total_tokens for entry in entries) / total_tokens
        if total_tokens
        else None
    )
    task_ids = {entry.task.task_id for entry in entries}
    return {
        "aggregate_ter": round(weighted_ter, 4) if weighted_ter is not None else None,
        "efficiency_status": "measured" if total_tokens else "insufficient-token-evidence",
        "aligned_tokens": sum(entry.aligned_tokens for entry in entries),
        "waste_tokens": sum(entry.waste_tokens for entry in entries),
        "total_tokens": total_tokens,
        "measurement_count": len(entries),
        "task_count": len(task_ids),
        "verified_success_tasks": len(
            {
                entry.task.task_id
                for entry in entries
                if _outcome_status(entry.task) == "verified-success"
            }
        ),
    }


def _group(
    entries: list[_MeasurementView], key: Callable[[_MeasurementView], str]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[_MeasurementView]] = {}
    for entry in entries:
        grouped.setdefault(key(entry), []).append(entry)
    return {name: _aggregate(items) for name, items in sorted(grouped.items())}


def linked_measurement_report(ledger: OutcomeLedger, repository_root: Path) -> dict[str, Any]:
    """Aggregate persisted real-session measurements and verified outcomes.

    The report never creates or migrates the ledger, even when given a writable
    ``OutcomeLedger`` instance.
    """
    if not ledger.path.is_file():
        return _empty_linked_report(ledger.repository_id(repository_root))
    with ledger.evidence_read() as diagnostics:
        return _linked_measurement_report(ledger, repository_root, diagnostics)


def _linked_measurement_report(
    ledger: OutcomeLedger,
    repository_root: Path,
    diagnostics: LedgerDiagnostics,
) -> dict[str, Any]:
    linked_ledger = _load_linked_ledger(ledger, repository_root)
    links_by_id = {
        link.session_link_id: (task, attempt, link) for task, attempt, link in linked_ledger.links
    }
    tasks_by_id = {task.task_id: task for task in linked_ledger.tasks}
    entries: list[_MeasurementView] = []
    valid_measurement_links: set[str] = set()
    invalid_measurements = 0

    for attempt in linked_ledger.attempts:
        task = tasks_by_id[attempt.task_id]
        for measurement in ledger.ter_measurements(attempt.attempt_id):
            if measurement.attempt_id != attempt.attempt_id:
                raise LinkedEfficiencyError(
                    f"Measurement {measurement.measurement_id} has an invalid attempt association"
                )
            if measurement.session_link_id is None:
                invalid_measurements += 1
                continue
            measurement_link = links_by_id.get(measurement.session_link_id)
            if measurement_link is None:
                if measurement.session_link_id in diagnostics.quarantined_session_link_ids:
                    diagnostics.excluded_measurements += 1
                    continue
                raise LinkedEfficiencyError(
                    f"Measurement {measurement.measurement_id} has an invalid session association"
                )
            measurement_task, measurement_attempt, measurement_session = measurement_link
            if (
                measurement_task.task_id != task.task_id
                or measurement_attempt.attempt_id != attempt.attempt_id
                or measurement_session.attempt_id != attempt.attempt_id
            ):
                raise LinkedEfficiencyError(
                    f"Measurement {measurement.measurement_id} has an invalid outcome association"
                )
            view = _measurement_view(task, attempt, measurement_session, measurement)
            if view is None:
                invalid_measurements += 1
                continue
            entries.append(view)
            valid_measurement_links.add(measurement_session.session_link_id)

    measured_task_ids = {
        task.task_id
        for task, _, link in linked_ledger.links
        if link.session_link_id in valid_measurement_links
    }
    linked_task_ids = {task.task_id for task, _, _ in linked_ledger.links}
    verified = [_outcome_success(task) for task in linked_ledger.tasks]
    verified_values = [value for value in verified if value is not None]
    aggregate_entries = _canonical_entries(entries)
    selected_measurement_ids = {entry.measurement.measurement_id for entry in aggregate_entries}

    raw_measurements = [
        {
            "measurement_id": entry.measurement.measurement_id,
            "task_id": entry.task.task_id,
            "attempt_id": entry.attempt.attempt_id,
            "routing_decision_id": entry.attempt.routing_decision_id,
            "session_link_id": entry.link.session_link_id,
            "route": _route_for_task(entry.task),
            "selected_model": entry.task.selected_model or "unassigned",
            "selected_capability_tier": (
                entry.task.selected_capability_tier.value
                if entry.task.selected_capability_tier
                else "unassigned"
            ),
            "analyzer": entry.measurement.analyzer,
            "analyzer_version": entry.measurement.analyzer_version,
            "measured_at": entry.measurement.measured_at.isoformat(),
            "task_category": entry.task.task_category.value,
            "routing_confidence": entry.task.routing_confidence,
            "outcome_verification": _outcome_status(entry.task),
            "aggregate_selected": (entry.measurement.measurement_id in selected_measurement_ids),
            "metrics": {
                "aggregate_ter": entry.aggregate_ter,
                "aligned_tokens": entry.aligned_tokens,
                "waste_tokens": entry.waste_tokens,
                "total_tokens": entry.total_tokens,
            },
        }
        for entry in entries
    ]
    raw_measurements.sort(
        key=lambda item: (
            item["task_id"],
            item["attempt_id"],
            item["session_link_id"],
            item["measurement_id"],
        )
    )

    missing_or_invalid_measurements = len(linked_ledger.links) - len(valid_measurement_links)
    overall: dict[str, Any] = {
        "total_tasks": len(linked_ledger.tasks),
        "linked_tasks": len(linked_task_ids),
        "successfully_measured_sessions": len(valid_measurement_links),
        "raw_measurement_count": len(entries),
        "superseded_measurements": len(entries) - len(aggregate_entries),
        "verified_outcome_tasks": len(verified_values),
        "verified_success_tasks": sum(verified_values),
        "verified_failure_tasks": sum(not value for value in verified_values),
        "unverified_outcome_tasks": len(verified) - len(verified_values),
        "invalid_measurements": invalid_measurements,
        "missing_or_invalid_measurements": missing_or_invalid_measurements,
        "linked_sessions_without_path": sum(
            link.session_path is None for _, _, link in linked_ledger.links
        ),
        **_aggregate(aggregate_entries),
    }
    overall["measured_tasks"] = len(measured_task_ids)
    overall["invalid_records"] = diagnostics.to_dict()

    return {
        "source": "linked-ter",
        "status": "empty" if not linked_ledger.tasks else "observational",
        "overall": overall,
        "aggregates": {
            "by_route": _group(aggregate_entries, lambda entry: _route_for_task(entry.task)),
            "by_model": _group(
                aggregate_entries,
                lambda entry: entry.task.selected_model or "unassigned",
            ),
            "by_capability_tier": _group(
                aggregate_entries,
                lambda entry: (
                    entry.task.selected_capability_tier.value
                    if entry.task.selected_capability_tier
                    else "unassigned"
                ),
            ),
        },
        "measurements": raw_measurements,
    }


def _empty_linked_report(repository_id: str) -> dict[str, Any]:
    """Documented empty result for a repository with no outcome ledger."""
    return {
        "source": "linked-ter",
        "status": "empty",
        "overall": {
            "total_tasks": 0,
            "linked_tasks": 0,
            "successfully_measured_sessions": 0,
            "raw_measurement_count": 0,
            "superseded_measurements": 0,
            "verified_outcome_tasks": 0,
            "verified_success_tasks": 0,
            "verified_failure_tasks": 0,
            "unverified_outcome_tasks": 0,
            "invalid_measurements": 0,
            "missing_or_invalid_measurements": 0,
            "linked_sessions_without_path": 0,
            "measured_tasks": 0,
            "invalid_records": LedgerDiagnostics().to_dict(),
            **_aggregate([]),
        },
        "aggregates": {
            "by_route": {},
            "by_model": {},
            "by_capability_tier": {},
        },
        "measurements": [],
    }
