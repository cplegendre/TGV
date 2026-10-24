from __future__ import annotations

import os
import stat
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tgv.bench.observed import (
    ObservedBenchmarkError,
    SessionObservation,
    TaskObservation,
    _load_observations,
    _wilson_interval,
)
from tgv.exceptions import TGVError
from tgv.linked_efficiency import _aggregate, _MeasurementView, _route_for_task
from tgv.models import Route, TGVConfig
from tgv.outcomes import LedgerDiagnostics, OutcomeLedger

SUPPORTED_ANALYZER = "TER CLI"
SUPPORTED_ANALYZER_VERSION = "3.0.0"
MINIMUM_VERIFIED_OUTCOMES = 20
MINIMUM_MEASURED_SESSIONS = 20
MINIMUM_PAIRED_TASKS = 20
SUCCESS_FLOOR = 0.90
_TARGETS = (
    (Route.LOCAL_FAST, "local_answer_confidence"),
    (Route.LOCAL_CODE, "local_code_confidence"),
)
_VALID_DECISION_SOURCES = {"deterministic", "model", "scoreboard", "safety-override"}
_CALIBRATION_THRESHOLDS = {
    "local_answer_confidence",
    "local_code_confidence",
}


class EvidenceCalibrationError(TGVError):
    """Raised when persisted evidence cannot be calibrated safely."""


@dataclass(frozen=True)
class EvidenceCalibrationProposal:
    target: str
    threshold_name: str
    current_threshold: float
    proposed_threshold: float
    direction: str
    status: str
    application_permitted: bool
    eligible_task_count: int
    verification_coverage: float | None
    evidence_availability: dict[str, Any]
    invalid_measurement_count: int
    verified_outcome_count: int
    verified_success_count: int
    verified_failure_count: int
    unverified_task_count: int
    incomplete_task_count: int
    paired_task_count: int
    verified_success_rate: float | None
    verified_success_confidence_interval_95: list[float] | None
    correctness_gate: dict[str, Any]
    efficiency_measurement_count: int
    aggregate_ter: float | None
    waste_ratio: float | None
    aligned_tokens: int
    waste_tokens: int
    total_tokens: int
    analyzer: str | None
    analyzer_version: str | None
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "threshold_name": self.threshold_name,
            "current_threshold": self.current_threshold,
            "proposed_threshold": self.proposed_threshold,
            "direction": self.direction,
            "status": self.status,
            "application_permitted": self.application_permitted,
            "eligible_task_count": self.eligible_task_count,
            "verification_coverage": self.verification_coverage,
            "evidence_availability": self.evidence_availability,
            "invalid_measurement_count": self.invalid_measurement_count,
            "verified_outcome_count": self.verified_outcome_count,
            "verified_success_count": self.verified_success_count,
            "verified_failure_count": self.verified_failure_count,
            "unverified_task_count": self.unverified_task_count,
            "incomplete_task_count": self.incomplete_task_count,
            "paired_task_count": self.paired_task_count,
            "verified_success_rate": self.verified_success_rate,
            "verified_success_confidence_interval_95": (
                self.verified_success_confidence_interval_95
            ),
            "correctness_gate": self.correctness_gate,
            "efficiency_measurement_count": self.efficiency_measurement_count,
            "aggregate_ter": self.aggregate_ter,
            "waste_ratio": self.waste_ratio,
            "aligned_tokens": self.aligned_tokens,
            "waste_tokens": self.waste_tokens,
            "total_tokens": self.total_tokens,
            "analyzer": self.analyzer,
            "analyzer_version": self.analyzer_version,
            "rationale": self.rationale,
        }


def _decision_source(task: TaskObservation) -> str:
    value = task.task.metadata.get("routing_decision_source")
    if not isinstance(value, str) or not value.strip():
        return "unavailable"
    return value if value in _VALID_DECISION_SOURCES else "invalid-decision-source"


def _selected_entries(
    sessions: list[SessionObservation], scope: tuple[str, str] | None
) -> list[_MeasurementView]:
    return [
        session.measurement
        for session in sessions
        if session.measurement is not None
        and scope is not None
        and (
            session.measurement.measurement.analyzer,
            session.measurement.measurement.analyzer_version,
        )
        == scope
    ]


def _proposal_for(
    route: Route,
    threshold_name: str,
    current_threshold: float,
    tasks: list[TaskObservation],
    selected_scope: tuple[str, str] | None,
) -> EvidenceCalibrationProposal:
    sessions = [session for task in tasks for session in task.sessions]
    entries = _selected_entries(sessions, selected_scope)
    measured_task_ids = {entry.task.task_id for entry in entries}
    outcome_counts = Counter(task.outcome_status for task in tasks)
    verified_success = outcome_counts["verified-success"]
    verified_failure = outcome_counts["verified-failure"]
    verified_outcomes = verified_success + verified_failure
    paired_tasks = sum(
        task.outcome_status in {"verified-success", "verified-failure"}
        and task.task.task_id in measured_task_ids
        for task in tasks
    )
    interval = _wilson_interval(verified_success, verified_outcomes)
    lower_bound = interval[0] if interval is not None else None
    correctness_passed = (
        verified_outcomes >= MINIMUM_VERIFIED_OUTCOMES
        and lower_bound is not None
        and lower_bound >= SUCCESS_FLOOR
    )
    aggregate = _aggregate(entries) if entries else None
    total_tokens = int(aggregate["total_tokens"]) if aggregate is not None else 0
    waste_tokens = int(aggregate["waste_tokens"]) if aggregate is not None else 0
    waste_ratio = (waste_tokens / total_tokens if total_tokens else 0.0) if entries else None
    analyzer, analyzer_version = selected_scope if selected_scope is not None else (None, None)
    supported_analyzer = (analyzer, analyzer_version) == (
        SUPPORTED_ANALYZER,
        SUPPORTED_ANALYZER_VERSION,
    )

    if verified_outcomes < MINIMUM_VERIFIED_OUTCOMES:
        status = "insufficient-evidence"
        rationale = (
            f"{verified_outcomes} verified outcomes is below the required "
            f"{MINIMUM_VERIFIED_OUTCOMES}; no change proposed."
        )
    elif len(entries) < MINIMUM_MEASURED_SESSIONS or paired_tasks < MINIMUM_PAIRED_TASKS:
        status = "efficiency-evidence-missing"
        rationale = (
            f"Comparable evidence has {len(entries)} measured sessions across "
            f"{paired_tasks} paired verified tasks; at least "
            f"{MINIMUM_MEASURED_SESSIONS} of each is required."
        )
    elif not supported_analyzer:
        status = "unsupported-analyzer"
        rationale = (
            f"Analyzer {analyzer or 'unavailable'} {analyzer_version or 'unavailable'} "
            f"is unsupported; only {SUPPORTED_ANALYZER} {SUPPORTED_ANALYZER_VERSION} "
            "is eligible for calibration."
        )
    elif not correctness_passed:
        status = "correctness-gate-failed"
        rationale = (
            f"The verified-success lower confidence bound {lower_bound:.10f} is below "
            f"the required success floor {SUCCESS_FLOOR:.2f}; no change proposed."
        )
    else:
        status = "no-change"
        rationale = "Descriptive evidence requirements are met."
    rationale += (
        " Observational efficiency does not establish the effect of rejecting model "
        "suggestions: deterministic fallback may select the same or a less capable route. "
        "No threshold intervention is justified; the configured threshold is unchanged."
    )

    return EvidenceCalibrationProposal(
        target=route.value,
        threshold_name=threshold_name,
        current_threshold=current_threshold,
        proposed_threshold=current_threshold,
        direction="none",
        status=status,
        application_permitted=False,
        eligible_task_count=len(tasks),
        verification_coverage=round(verified_outcomes / len(tasks), 4) if tasks else None,
        evidence_availability={
            "verified_sample_minimum_met": verified_outcomes >= MINIMUM_VERIFIED_OUTCOMES,
            "measurement_sample_minimum_met": len(entries) >= MINIMUM_MEASURED_SESSIONS,
            "paired_sample_minimum_met": paired_tasks >= MINIMUM_PAIRED_TASKS,
            "supported_analyzer": supported_analyzer,
        },
        invalid_measurement_count=sum(session.invalid_measurement_count for session in sessions),
        verified_outcome_count=verified_outcomes,
        verified_success_count=verified_success,
        verified_failure_count=verified_failure,
        unverified_task_count=outcome_counts["unverified"],
        incomplete_task_count=outcome_counts["incomplete"],
        paired_task_count=paired_tasks,
        verified_success_rate=(
            round(verified_success / verified_outcomes, 4) if verified_outcomes else None
        ),
        verified_success_confidence_interval_95=(
            [round(bound, 4) for bound in interval] if interval is not None else None
        ),
        correctness_gate={
            "passed": correctness_passed,
            "minimum_verified_outcomes": MINIMUM_VERIFIED_OUTCOMES,
            "success_floor": SUCCESS_FLOOR,
            "lower_confidence_bound": lower_bound,
            "comparison_uses_unrounded_bound": True,
            "uses_wilson_lower_bound": True,
        },
        efficiency_measurement_count=len(entries),
        aggregate_ter=(float(aggregate["aggregate_ter"]) if aggregate is not None else None),
        waste_ratio=round(waste_ratio, 4) if waste_ratio is not None else None,
        aligned_tokens=int(aggregate["aligned_tokens"]) if aggregate is not None else 0,
        waste_tokens=waste_tokens,
        total_tokens=total_tokens,
        analyzer=analyzer,
        analyzer_version=analyzer_version,
        rationale=rationale,
    )


def _policy() -> dict[str, Any]:
    return {
        "eligible_decision_source": "model",
        "eligible_routes": [route.value for route, _ in _TARGETS],
        "minimum_verified_outcomes": MINIMUM_VERIFIED_OUTCOMES,
        "minimum_measured_sessions": MINIMUM_MEASURED_SESSIONS,
        "minimum_paired_tasks": MINIMUM_PAIRED_TASKS,
        "success_floor": SUCCESS_FLOOR,
        "success_gate": "95% Wilson score lower confidence bound",
        "intervention_policy": "diagnostics-only; no supported directional intervention",
        "supported_analyzer": SUPPORTED_ANALYZER,
        "supported_analyzer_version": SUPPORTED_ANALYZER_VERSION,
        "threshold_lowering_permitted": False,
        "correctness_precedence": True,
    }


def _empty_report(repository_id: str, config: TGVConfig) -> dict[str, Any]:
    proposals = [
        _proposal_for(route, name, getattr(config.routing, name), [], None)
        for route, name in _TARGETS
    ]
    return _serialize_report(repository_id, None, proposals, [], Counter[str](), Counter[str]())


def _serialize_report(
    repository_id: str,
    selected_scope: tuple[str, str] | None,
    proposals: list[EvidenceCalibrationProposal],
    tasks: list[TaskObservation],
    route_counts: Counter[str],
    source_counts: Counter[str],
    unattributed_invalid_measurements: int = 0,
    invalid_records: dict[str, Any] | None = None,
) -> dict[str, Any]:
    linked_tasks = sum(bool(task.sessions) for task in tasks)
    return {
        "source": "evidence-ledger",
        "repository_id": repository_id,
        "selected_analyzer": selected_scope[0] if selected_scope else None,
        "selected_analyzer_version": selected_scope[1] if selected_scope else None,
        "coverage": {
            "eligible_task_count": sum(
                _route_for_task(task.task) in {route.value for route, _ in _TARGETS}
                and _decision_source(task) == "model"
                for task in tasks
            ),
            "total_task_count": len(tasks),
            "linked_task_count": linked_tasks,
            "invalid_measurement_count": unattributed_invalid_measurements
            + sum(session.invalid_measurement_count for task in tasks for session in task.sessions),
            "unattributed_invalid_measurements": unattributed_invalid_measurements,
            "route_counts": dict(sorted(route_counts.items())),
            "routing_decision_source_counts": dict(sorted(source_counts.items())),
            "invalid_records": invalid_records
            if invalid_records is not None
            else LedgerDiagnostics().to_dict(),
            "excluded_task_count": len(tasks)
            - sum(
                _route_for_task(task.task) in {route.value for route, _ in _TARGETS}
                and _decision_source(task) == "model"
                for task in tasks
            ),
        },
        "policy": _policy(),
        "proposals": [proposal.to_dict() for proposal in proposals],
        "applied": {
            "requested": False,
            "changed": False,
            "thresholds": {},
        },
        "limitations": [
            "Evidence is observational and may be affected by selection bias.",
            "Task difficulty and model-selection effects are not controlled.",
            "Verification coverage and missing TER measurements can bias observations.",
            "No counterfactual routing executions are available.",
            "TER efficiency is not verified correctness.",
            "Repeated attempts contribute session-level token weight, not independent outcomes.",
            "Efficiency includes failed, unverified and incomplete attempts in eligible tasks.",
            "Paired evidence joins at task level, not necessarily the successful attempt.",
            "Minimum sample counts do not establish causal validity.",
            "Historical route or decision-source metadata may be missing; it is never inferred.",
            "Evidence-calibration does not propose numerical threshold changes.",
        ],
    }


def evidence_calibration(
    ledger: OutcomeLedger,
    config: TGVConfig,
    repository_root: Path,
) -> dict[str, Any]:
    """Produce read-only, evidence-gated threshold proposals."""
    repository_id = ledger.repository_id(repository_root)
    if not ledger.path.is_file():
        return _empty_report(repository_id, config)

    read_only_ledger = ledger if ledger.read_only else OutcomeLedger(ledger.path, read_only=True)
    try:
        data = _load_observations(read_only_ledger, repository_root)
    except ObservedBenchmarkError as exc:
        raise EvidenceCalibrationError(f"Cannot calibrate outcome evidence: {exc}") from exc

    route_counts = Counter(_route_for_task(task.task) for task in data.tasks)
    source_counts = Counter(_decision_source(task) for task in data.tasks)
    proposals: list[EvidenceCalibrationProposal] = []
    for route, threshold_name in _TARGETS:
        tasks = [
            task
            for task in data.tasks
            if _route_for_task(task.task) == route.value and _decision_source(task) == "model"
        ]
        proposals.append(
            _proposal_for(
                route,
                threshold_name,
                getattr(config.routing, threshold_name),
                tasks,
                data.selected_analyzer_scope,
            )
        )
    return _serialize_report(
        repository_id,
        data.selected_analyzer_scope,
        proposals,
        data.tasks,
        route_counts,
        source_counts,
        data.unattributed_invalid_measurements,
        data.diagnostics.to_dict(),
    )


@contextmanager
def _configuration_lock(config_path: Path) -> Iterator[None]:
    # Never unlink the sidecar: replacing its inode would split the lock domain.
    try:
        import fcntl
    except ImportError as exc:
        raise EvidenceCalibrationError(
            "Configuration application requires POSIX advisory locks"
        ) from exc
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _configuration_snapshot(config_path: Path) -> tuple[bytes, os.stat_result]:
    if not stat.S_ISREG(config_path.lstat().st_mode):
        raise EvidenceCalibrationError("Configuration must be a regular file, not a symbolic link")
    descriptor = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        return stream.read(), info


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def apply_evidence_calibration(
    report: dict[str, Any],
    config_path: Path,
    original_config: TGVConfig,
    original_bytes: bytes,
) -> dict[str, Any]:
    """Apply only eligible proposals with one validated atomic configuration write."""
    if config_path.is_symlink():
        raise EvidenceCalibrationError("Configuration must be a regular file, not a symbolic link")
    eligible = [
        proposal
        for proposal in report["proposals"]
        if proposal["application_permitted"]
        and proposal["threshold_name"]
        and proposal["proposed_threshold"] is not None
    ]
    result = {**report, "applied": {"requested": True, "changed": False, "thresholds": {}}}
    if not eligible:
        return result

    # Resolve directory aliases to one stable lock path, without following the target.
    config_path = config_path.parent.resolve() / config_path.name
    try:
        with _configuration_lock(config_path):
            return _apply_locked(eligible, result, config_path, original_config, original_bytes)
    except OSError as exc:
        raise EvidenceCalibrationError(
            f"Cannot atomically update configuration {config_path}: {exc}"
        ) from exc


def _apply_locked(
    eligible: list[dict[str, Any]],
    result: dict[str, Any],
    config_path: Path,
    original_config: TGVConfig,
    original_bytes: bytes,
) -> dict[str, Any]:
    try:
        current_bytes, original_stat = _configuration_snapshot(config_path)
    except OSError as exc:
        raise EvidenceCalibrationError(f"Cannot read configuration {config_path}: {exc}") from exc
    if current_bytes != original_bytes:
        raise EvidenceCalibrationError(
            f"Configuration changed during calibration: {config_path}; no changes were applied"
        )

    raw: Any
    try:
        raw = yaml.safe_load(current_bytes.decode("utf-8"))
        current_config = TGVConfig.model_validate(raw)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise EvidenceCalibrationError(f"Invalid configuration {config_path}: {exc}") from exc
    if current_config != original_config:
        raise EvidenceCalibrationError(
            f"Configuration changed during calibration: {config_path}; no changes were applied"
        )

    changes: dict[str, float] = {}
    for proposal in eligible:
        threshold_name = str(proposal["threshold_name"])
        if threshold_name not in _CALIBRATION_THRESHOLDS:
            raise EvidenceCalibrationError(f"Unsupported calibration threshold: {threshold_name}")
        proposed_threshold = float(proposal["proposed_threshold"])
        if not 0.0 <= proposed_threshold <= 1.0:
            raise EvidenceCalibrationError(
                f"Invalid proposed threshold for {threshold_name}: {proposed_threshold}"
            )
        if proposal["current_threshold"] != getattr(current_config.routing, threshold_name):
            raise EvidenceCalibrationError(
                f"Configuration threshold changed for {threshold_name}; no changes were applied"
            )
        if proposed_threshold <= getattr(current_config.routing, threshold_name):
            raise EvidenceCalibrationError("An eligible proposal must strictly raise its threshold")
        setattr(current_config.routing, threshold_name, proposed_threshold)
        changes[threshold_name] = proposed_threshold

    serialized = yaml.safe_dump(current_config.model_dump(mode="json"), sort_keys=False)
    try:
        validated = TGVConfig.model_validate(yaml.safe_load(serialized))
    except (yaml.YAMLError, ValueError) as exc:
        raise EvidenceCalibrationError(
            f"Calibrated configuration failed validation for {config_path}: {exc}"
        ) from exc
    if validated.routing.local_answer_confidence != (
        current_config.routing.local_answer_confidence
    ) or validated.routing.local_code_confidence != (current_config.routing.local_code_confidence):
        raise EvidenceCalibrationError(
            f"Calibrated configuration did not round-trip for {config_path}"
        )

    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, original_stat.st_mode & 0o777)
        latest_bytes, latest_stat = _configuration_snapshot(config_path)
        if latest_bytes != original_bytes or _file_identity(latest_stat) != _file_identity(
            original_stat
        ):
            raise EvidenceCalibrationError(
                f"Configuration changed during calibration: {config_path}; no changes were applied"
            )
        # Advisory locking protects cooperating applications. An external writer can
        # still race this final check; replacement is atomic, not a filesystem CAS.
        os.replace(temporary_path, config_path)
        temporary_name = None
    except OSError as exc:
        raise EvidenceCalibrationError(
            f"Cannot atomically update configuration {config_path}: {exc}"
        ) from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)

    result["applied"] = {
        "requested": True,
        "changed": True,
        "thresholds": changes,
    }
    return result


def render_evidence_calibration(report: dict[str, Any]) -> str:
    lines = [
        "source: evidence-ledger",
        (
            f"selected analyzer: {report['selected_analyzer'] or 'unavailable'} "
            f"{report['selected_analyzer_version'] or 'unavailable'}"
        ),
        (
            f"eligible tasks: {report['coverage']['eligible_task_count']} "
            f"excluded: {report['coverage']['excluded_task_count']}"
        ),
    ]
    for proposal in report["proposals"]:
        displayed = {
            key: proposal[key] if proposal[key] is not None else "unavailable"
            for key in ("verification_coverage", "aggregate_ter", "waste_ratio")
        }
        lines.append(
            f"{proposal['target']}: status={proposal['status']} "
            f"threshold={proposal['current_threshold']:.2f} "
            f"proposed={proposal['proposed_threshold']:.2f} "
            f"verified={proposal['verified_outcome_count']} "
            f"measured={proposal['efficiency_measurement_count']} - "
            f"{proposal['rationale']}"
        )
        lines.extend(
            [
                f"  outcomes: eligible={proposal['eligible_task_count']} "
                f"success={proposal['verified_success_count']} "
                f"failure={proposal['verified_failure_count']} "
                f"unverified={proposal['unverified_task_count']} "
                f"incomplete={proposal['incomplete_task_count']} "
                f"verification-coverage={displayed['verification_coverage']}",
                f"  correctness: passed={proposal['correctness_gate']['passed']} "
                f"Wilson-95%={proposal['verified_success_confidence_interval_95']} "
                "(gate compares the unrounded lower bound)",
                f"  efficiency: measured={proposal['efficiency_measurement_count']} "
                f"paired-tasks={proposal['paired_task_count']} "
                f"invalid={proposal['invalid_measurement_count']} "
                f"TER={displayed['aggregate_ter']} waste={displayed['waste_ratio']}",
                f"  intervention: application-permitted={proposal['application_permitted']}",
            ]
        )
    lines.extend(f"limitation: {limitation}" for limitation in report["limitations"])
    if report["applied"]["changed"]:
        lines.append(
            "applied thresholds: "
            + ", ".join(
                f"{name}={value:.2f}"
                for name, value in sorted(report["applied"]["thresholds"].items())
            )
        )
    else:
        lines.append("applied thresholds: none")
    return "\n".join(lines)
