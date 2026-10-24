from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from tgv.exceptions import (
    ConflictingMeasurementError,
    ConflictingSessionLinkError,
    CorrelationMismatchError,
    CrossRepositoryError,
    LedgerReadError,
    LedgerWriteError,
)
from tgv.models import (
    AgentSessionLink,
    CapabilityTier,
    ExecutionAttempt,
    OutcomeTask,
    Route,
    TaskCategory,
    TaskProfile,
    TerMeasurement,
)

_SCHEMA_VERSION = 3
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_ROW_DECODE_ERRORS = (KeyError, IndexError, TypeError, ValueError)
_RowT = TypeVar("_RowT")

ATTEMPT_STATUSES = frozenset({"pending", "running", "completed", "failed"})
_ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "completed", "failed"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
}
_TER_METRIC_FIELDS = frozenset({"aggregate_ter", "aligned_tokens", "waste_tokens", "total_tokens"})

_INVALID_RECORD_POLICY = (
    "unreadable records are quarantined with their dependent attempts, sessions, and "
    "measurements; quarantined records are excluded from every denominator and are never "
    "counted as verified successes or failures"
)


def safe_json_object(raw: Any) -> dict[str, Any] | None:
    """Decode a persisted JSON object without raising or echoing the raw value."""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _coerce_finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row_value(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


@dataclass
class LedgerDiagnostics:
    """Counts of quarantined or repaired records observed during an evidence read."""

    excluded_tasks: int = 0
    excluded_attempts: int = 0
    excluded_sessions: int = 0
    excluded_measurements: int = 0
    invalid_metadata_tasks: int = 0
    invalid_confidence_tasks: int = 0
    invalid_measurement_metrics: int = 0
    quarantined_task_ids: set[str] = field(default_factory=set)
    quarantined_session_link_ids: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": _INVALID_RECORD_POLICY,
            "excluded_tasks": self.excluded_tasks,
            "excluded_attempts": self.excluded_attempts,
            "excluded_sessions": self.excluded_sessions,
            "excluded_measurements": self.excluded_measurements,
            "invalid_metadata_tasks": self.invalid_metadata_tasks,
            "invalid_confidence_tasks": self.invalid_confidence_tasks,
            "invalid_measurement_metrics": self.invalid_measurement_metrics,
        }


@dataclass(frozen=True)
class TerMeasurementRequest:
    """A measurement to persist, without generated database identifiers."""

    attempt_id: str
    analyzer: str
    analyzer_version: str
    metrics: Mapping[str, float | int | None]
    session_link_id: str | None = None
    measured_at: datetime | None = None


def _normalize_session_link(
    agent: str,
    external_session_id: str,
    content_hash: str | None,
    session_path: str | None,
) -> tuple[str, str, str | None, str | None]:
    agent_name = agent.strip()
    session_id = external_session_id.strip()
    if not agent_name:
        raise ValueError("Agent name must not be empty")
    if not session_id:
        raise ValueError("External session id must not be empty")
    normalized_hash = None if content_hash is None else content_hash.lower()
    if normalized_hash is not None and not _CONTENT_HASH.fullmatch(normalized_hash):
        raise ValueError("content_hash must be a sha256 hex digest")
    normalized_path = (
        None if session_path is None else str(Path(session_path).expanduser().absolute())
    )
    return agent_name, session_id, normalized_hash, normalized_path


def _normalize_attempt_status(status: str, *, current: str | None = None) -> str:
    normalized = status.strip()
    if not normalized:
        raise ValueError("Execution attempt status must not be empty")
    if normalized not in ATTEMPT_STATUSES:
        supported = ", ".join(sorted(ATTEMPT_STATUSES))
        raise ValueError(
            f"Unsupported execution attempt status {normalized!r}; expected one of: {supported}"
        )
    if current is None or normalized == current:
        return normalized
    allowed = _ATTEMPT_TRANSITIONS.get(current)
    if allowed is None:
        raise ValueError(
            f"Execution attempt has unsupported historical status {current!r}; "
            "refusing an automatic status change"
        )
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(
            f"Invalid execution attempt transition {current!r} -> {normalized!r}; "
            f"allowed from {current!r}: {allowed_text}"
        )
    return normalized


class OutcomeLedger:
    """Privacy-minimal SQLite outcome store with transactional schema initialization."""

    def __init__(
        self, path: Path, *, retain_raw_prompts: bool = False, read_only: bool = False
    ) -> None:
        self.path = path
        self.retain_raw_prompts = retain_raw_prompts
        self.read_only = read_only
        self._force_read_only = False
        self._diagnostics: LedgerDiagnostics | None = None

    def _connect(self) -> sqlite3.Connection:
        if self.read_only or self._force_read_only:
            return self._connect_read_only()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            with suppress(OSError):
                os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            self._initialize(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @property
    def diagnostics(self) -> LedgerDiagnostics | None:
        """Diagnostics for the active evidence read, if one is in progress."""
        return self._diagnostics

    @contextmanager
    def evidence_read(self) -> Iterator[LedgerDiagnostics]:
        """Read evidence through read-only connections and quarantine invalid rows.

        Every connection opened inside this context uses SQLite ``mode=ro``, so no
        database, schema, or record is created or modified even when the ledger object
        was constructed as writable. Nested use shares the outer context.
        """
        if self._diagnostics is not None:
            yield self._diagnostics
            return
        diagnostics = LedgerDiagnostics()
        previous = self._force_read_only
        self._force_read_only = True
        self._diagnostics = diagnostics
        try:
            yield diagnostics
        finally:
            self._diagnostics = None
            self._force_read_only = previous

    def _connect_read_only(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise LedgerReadError(f"Outcome ledger does not exist: {self.path}")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA query_only = ON")
            self._validate_read_only_schema(connection)
        except (sqlite3.Error, LedgerReadError) as exc:
            if connection is not None:
                connection.close()
            raise LedgerReadError(f"Outcome ledger cannot be opened read-only: {exc}") from exc
        return connection

    @staticmethod
    def _validate_read_only_schema(connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        required_tables = {
            "schema_version",
            "outcomes",
            "execution_attempts",
            "agent_sessions",
            "ter_measurements",
            "session_locators",
        }
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            raise LedgerReadError(
                "Outcome ledger is missing required tables: " + ", ".join(missing_tables)
            )
        rows = connection.execute("SELECT version FROM schema_version").fetchall()
        if len(rows) != 1:
            raise LedgerReadError("Outcome ledger has an invalid schema version record")
        try:
            version = int(rows[0][0])
        except (TypeError, ValueError) as exc:
            raise LedgerReadError("Outcome ledger has an invalid schema version value") from exc
        if version != _SCHEMA_VERSION:
            raise LedgerReadError(
                f"Outcome ledger schema version {version} is not supported in read-only mode; "
                "upgrade the ledger with a writable TGV command"
            )

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                OutcomeLedger._create_v1_schema(connection)
                OutcomeLedger._create_v2_schema(connection)
                OutcomeLedger._create_v3_schema(connection)
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (_SCHEMA_VERSION,)
                )
                return
            version = int(row[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported outcome schema version: {version}")
            if version == 1:
                OutcomeLedger._migrate_v1_to_v2(connection)
                OutcomeLedger._migrate_v2_to_v3(connection)
                connection.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
                return
            if version == 2:
                OutcomeLedger._migrate_v2_to_v3(connection)
                connection.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
                return
            OutcomeLedger._create_v1_schema(connection)
            OutcomeLedger._create_v2_schema(connection)
            OutcomeLedger._create_v3_schema(connection)

    @staticmethod
    def _create_v1_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                task_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                repository_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                raw_request TEXT,
                task_category TEXT NOT NULL,
                initial_complexity_score REAL,
                revised_complexity_score REAL,
                routing_confidence REAL,
                selected_model TEXT,
                selected_capability_tier TEXT,
                routing_reasons TEXT NOT NULL DEFAULT '[]',
                retrieved_files TEXT NOT NULL DEFAULT '[]',
                retrieved_symbols TEXT NOT NULL DEFAULT '[]',
                context_token_estimate INTEGER,
                execution_status TEXT NOT NULL DEFAULT 'pending',
                tests_passed INTEGER,
                static_checks_passed INTEGER,
                human_accepted INTEGER,
                rework_required INTEGER,
                retry_count INTEGER NOT NULL DEFAULT 0,
                latency_seconds REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                estimated_cost REAL,
                failure_category TEXT,
                safety_violation INTEGER,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS outcomes_segment "
            "ON outcomes(task_category, selected_model, selected_capability_tier)"
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        OutcomeLedger._create_v1_schema(connection)
        OutcomeLedger._create_v2_schema(connection)
        connection.execute(
            "UPDATE outcomes SET routing_decision_id = lower(hex(randomblob(16))) "
            "WHERE routing_decision_id IS NULL AND selected_model IS NOT NULL"
        )

    @staticmethod
    def _create_v2_schema(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outcomes)")}
        if "routing_decision_id" not in columns:
            try:
                connection.execute("ALTER TABLE outcomes ADD COLUMN routing_decision_id TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS outcomes_routing_decision "
            "ON outcomes(routing_decision_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES outcomes(task_id) ON DELETE CASCADE,
                repository_id TEXT NOT NULL,
                routing_decision_id TEXT,
                attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE (task_id, attempt_number)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_link_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL
                    REFERENCES execution_attempts(attempt_id) ON DELETE CASCADE,
                agent TEXT NOT NULL CHECK (length(trim(agent)) > 0),
                external_session_id TEXT NOT NULL
                    CHECK (length(trim(external_session_id)) > 0),
                content_hash TEXT CHECK (content_hash IS NULL OR length(content_hash) = 64),
                linked_at TEXT NOT NULL,
                UNIQUE (agent, external_session_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS agent_sessions_attempt ON agent_sessions(attempt_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ter_measurements (
                measurement_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL
                    REFERENCES execution_attempts(attempt_id) ON DELETE CASCADE,
                session_link_id TEXT
                    REFERENCES agent_sessions(session_link_id) ON DELETE CASCADE,
                analyzer TEXT NOT NULL CHECK (length(trim(analyzer)) > 0),
                analyzer_version TEXT NOT NULL CHECK (length(trim(analyzer_version)) > 0),
                measured_at TEXT NOT NULL,
                metrics TEXT NOT NULL DEFAULT '{}',
                UNIQUE (attempt_id, analyzer, analyzer_version)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ter_measurements_attempt ON ter_measurements(attempt_id)"
        )

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        OutcomeLedger._create_v1_schema(connection)
        OutcomeLedger._create_v2_schema(connection)
        OutcomeLedger._create_v3_schema(connection)

    @staticmethod
    def _create_v3_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_locators (
                session_link_id TEXT PRIMARY KEY
                    REFERENCES agent_sessions(session_link_id) ON DELETE CASCADE,
                locator TEXT NOT NULL UNIQUE CHECK (length(trim(locator)) > 0)
            )
            """
        )

    @staticmethod
    def repository_id(root: Path) -> str:
        return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]

    def create_pending(
        self,
        request: str,
        repository_root: Path,
        profile: TaskProfile,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        request_hash = hashlib.sha256(request.encode()).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO outcomes (
                    task_id, timestamp, repository_id, request_hash, raw_request,
                    task_category, initial_complexity_score, selected_capability_tier, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    datetime.now(UTC).isoformat(),
                    self.repository_id(repository_root),
                    request_hash,
                    request if self.retain_raw_prompts else None,
                    profile.task_category.value,
                    profile.complexity_score,
                    profile.recommended_capability_tier.value,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
        return task_id

    def record_routing(
        self,
        task_id: str,
        *,
        revised_profile: TaskProfile,
        confidence: float,
        model: str,
        capability_tier: CapabilityTier,
        reasons: list[str],
        files: list[str],
        symbols: list[str],
        context_token_estimate: int,
        route: Route | None = None,
        decision_source: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT metadata FROM outcomes WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task id: {task_id}")
        stored_metadata = json.loads(row["metadata"])
        if not isinstance(stored_metadata, dict):
            raise ValueError(f"Outcome task {task_id} has invalid metadata")
        metadata = dict(stored_metadata)
        if route is not None:
            metadata["authoritative_route"] = route.value
        if decision_source is not None:
            metadata["routing_decision_source"] = decision_source
        else:
            metadata.pop("routing_decision_source", None)
        self.assign_routing_decision_id(task_id)
        values: dict[str, Any] = {
            "revised_complexity_score": revised_profile.complexity_score,
            "routing_confidence": confidence,
            "selected_model": model,
            "selected_capability_tier": capability_tier.value,
            "routing_reasons": json.dumps(reasons),
            "retrieved_files": json.dumps(files),
            "retrieved_symbols": json.dumps(symbols),
            "context_token_estimate": context_token_estimate,
        }
        values["metadata"] = json.dumps(metadata, sort_keys=True)
        self._update(task_id, **values)

    def record_execution(
        self,
        task_id: str,
        *,
        status: str,
        latency_seconds: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost: float | None = None,
        failure_category: str | None = None,
        safety_violation: bool | None = None,
    ) -> None:
        self._update(
            task_id,
            execution_status=status,
            latency_seconds=latency_seconds,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
            failure_category=failure_category,
            safety_violation=safety_violation,
        )

    def record_validation(
        self,
        task_id: str,
        *,
        tests_passed: bool | None,
        static_checks_passed: bool | None,
    ) -> None:
        """Record caller-attested validation results; TGV does not execute the checks."""
        self._update(
            task_id,
            tests_passed=tests_passed,
            static_checks_passed=static_checks_passed,
        )

    def record_human_feedback(
        self,
        task_id: str,
        *,
        accepted: bool | None,
        rework_required: bool | None,
    ) -> None:
        self._update(task_id, human_accepted=accepted, rework_required=rework_required)

    def complete_outcome(
        self,
        task_id: str,
        *,
        accepted: bool,
        expected_repository_id: str | None = None,
    ) -> int:
        """Complete a task and close its pending attempts in one transaction.

        The task status, human feedback, and pending-attempt lifecycle are committed
        together. Any validation or database failure rolls back every write.
        """
        try:
            connection = self._connect()
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
            raise LedgerWriteError("Outcome ledger cannot be opened for completion") from exc

        with closing(connection):
            try:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT repository_id FROM outcomes WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise KeyError(f"Unknown task id: {task_id}")
                repository_id = str(task["repository_id"])
                if expected_repository_id is not None and expected_repository_id != repository_id:
                    raise CrossRepositoryError(
                        "Task repository does not match the current repository"
                    )

                attempts = connection.execute(
                    "SELECT task_id, repository_id FROM execution_attempts WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
                if any(
                    row["task_id"] != task_id or row["repository_id"] != repository_id
                    for row in attempts
                ):
                    raise CorrelationMismatchError(
                        "Execution attempt correlation does not match its outcome task"
                    )

                cursor = connection.execute(
                    "UPDATE outcomes SET execution_status = ? WHERE task_id = ?",
                    ("completed", task_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Unknown task id: {task_id}")
                connection.execute(
                    "UPDATE outcomes SET human_accepted = ?, rework_required = ? WHERE task_id = ?",
                    (int(accepted), int(not accepted), task_id),
                )
                completed_at = datetime.now(UTC).isoformat()
                cursor = connection.execute(
                    "UPDATE execution_attempts SET status = 'completed', completed_at = ? "
                    "WHERE task_id = ? AND status = 'pending'",
                    (completed_at, task_id),
                )
                completed_attempts = cursor.rowcount
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LedgerWriteError("Outcome completion failed and was rolled back") from exc
            except BaseException:
                connection.rollback()
                raise
        return completed_attempts

    def record_retry(self, task_id: str, *, escalated: bool = False) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE outcomes SET retry_count = retry_count + 1, "
                "execution_status = ? WHERE task_id = ?",
                ("escalated" if escalated else "retrying", task_id),
            )

    def task_repository_id(self, task_id: str) -> str:
        """Return the repository identity recorded for an existing task."""
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT repository_id FROM outcomes WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task id: {task_id}")
        return str(row["repository_id"])

    def agent_session(self, agent: str, external_session_id: str) -> AgentSessionLink | None:
        """Return an existing external session link, if this agent/session is known."""
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT agent_sessions.*, session_locators.locator AS session_path "
                "FROM agent_sessions LEFT JOIN session_locators "
                "ON session_locators.session_link_id = agent_sessions.session_link_id "
                "WHERE agent = ? AND external_session_id = ?",
                (agent.strip(), external_session_id.strip()),
            ).fetchone()
        return None if row is None else self._row_to_session_link(row)

    def routing_decision_id_for(self, task_id: str) -> str | None:
        """Return the durable routing-decision identifier for a task, if any."""
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT routing_decision_id FROM outcomes WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task id: {task_id}")
        value = row["routing_decision_id"]
        return None if value is None else str(value)

    def assign_routing_decision_id(self, task_id: str) -> str:
        """Assign and return a stable routing-decision identifier for a task."""
        candidate = str(uuid.uuid4())
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE outcomes SET routing_decision_id = COALESCE(routing_decision_id, ?) "
                "WHERE task_id = ?",
                (candidate, task_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown task id: {task_id}")
            row = connection.execute(
                "SELECT routing_decision_id FROM outcomes WHERE task_id = ?", (task_id,)
            ).fetchone()
        return str(row["routing_decision_id"])

    def create_execution_attempt(
        self,
        task_id: str,
        *,
        expected_repository_id: str | None = None,
        started_at: datetime | None = None,
        status: str = "pending",
    ) -> ExecutionAttempt:
        """Create the next execution attempt for an existing task."""
        status = _normalize_attempt_status(status)
        last_error: sqlite3.IntegrityError | None = None
        for _ in range(3):
            try:
                return self._create_execution_attempt(
                    task_id,
                    expected_repository_id=expected_repository_id,
                    started_at=started_at,
                    status=status,
                )
            except sqlite3.IntegrityError as exc:
                last_error = exc
                continue
        raise RuntimeError(
            f"Could not allocate an execution attempt for task: {task_id}"
        ) from last_error

    def _create_execution_attempt(
        self,
        task_id: str,
        *,
        expected_repository_id: str | None,
        started_at: datetime | None,
        status: str,
    ) -> ExecutionAttempt:
        with closing(self._connect()) as connection, connection:
            return self._insert_execution_attempt(
                connection,
                task_id,
                expected_repository_id=expected_repository_id,
                started_at=started_at,
                status=status,
            )

    @staticmethod
    def _insert_execution_attempt(
        connection: sqlite3.Connection,
        task_id: str,
        *,
        expected_repository_id: str | None,
        started_at: datetime | None,
        status: str,
    ) -> ExecutionAttempt:
        task = connection.execute(
            "SELECT repository_id, routing_decision_id FROM outcomes WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise KeyError(f"Unknown task id: {task_id}")
        repository_id = str(task["repository_id"])
        if expected_repository_id is not None and expected_repository_id != repository_id:
            raise CrossRepositoryError(
                "Execution attempt repository does not match the task repository"
            )
        attempt_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
                "FROM execution_attempts WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )
        attempt = ExecutionAttempt(
            attempt_id=str(uuid.uuid4()),
            task_id=task_id,
            repository_id=repository_id,
            routing_decision_id=(
                None if task["routing_decision_id"] is None else str(task["routing_decision_id"])
            ),
            attempt_number=attempt_number,
            started_at=started_at or datetime.now(UTC),
            status=status.strip(),
        )
        connection.execute(
            "INSERT INTO execution_attempts (attempt_id, task_id, repository_id, "
            "routing_decision_id, attempt_number, started_at, completed_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.attempt_id,
                attempt.task_id,
                attempt.repository_id,
                attempt.routing_decision_id,
                attempt.attempt_number,
                attempt.started_at.isoformat(),
                None,
                attempt.status,
            ),
        )
        return attempt

    def complete_pending_attempts(self, task_id: str) -> int:
        """Close pending attempts after explicit task completion, atomically.

        Existing completed/failed attempts and historical timestamps are preserved.
        """
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE execution_attempts SET status = 'completed', completed_at = ? "
                "WHERE task_id = ? AND status = 'pending'",
                (datetime.now(UTC).isoformat(), task_id),
            )
            return cursor.rowcount

    def update_execution_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        completed_at: datetime | None = None,
    ) -> None:
        """Record a new status for an existing execution attempt."""
        requested = _normalize_attempt_status(status)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown attempt id: {attempt_id}")
                current = str(row["status"])
                normalized = _normalize_attempt_status(requested, current=current)
                if completed_at is None:
                    connection.execute(
                        "UPDATE execution_attempts SET status = ? WHERE attempt_id = ?",
                        (normalized, attempt_id),
                    )
                else:
                    connection.execute(
                        "UPDATE execution_attempts SET status = ?, completed_at = ? "
                        "WHERE attempt_id = ?",
                        (normalized, completed_at.isoformat(), attempt_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def link_agent_session(
        self,
        attempt_id: str,
        *,
        agent: str,
        external_session_id: str,
        content_hash: str | None = None,
        session_path: str | None = None,
        linked_at: datetime | None = None,
    ) -> AgentSessionLink:
        """Link an opaque external agent-session reference to an execution attempt."""
        agent_name, session_id, normalized_hash, normalized_path = _normalize_session_link(
            agent, external_session_id, content_hash, session_path
        )
        with closing(self._connect()) as connection, connection:
            attempt = connection.execute(
                "SELECT attempt_id FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(f"Unknown attempt id: {attempt_id}")
            return self._insert_agent_session(
                connection,
                attempt_id=attempt_id,
                agent=agent_name,
                external_session_id=session_id,
                content_hash=normalized_hash,
                session_path=normalized_path,
                linked_at=linked_at,
            )

    @staticmethod
    def _insert_agent_session(
        connection: sqlite3.Connection,
        *,
        attempt_id: str,
        agent: str,
        external_session_id: str,
        content_hash: str | None,
        session_path: str | None,
        linked_at: datetime | None,
    ) -> AgentSessionLink:
        link = AgentSessionLink(
            session_link_id=str(uuid.uuid4()),
            attempt_id=attempt_id,
            agent=agent,
            external_session_id=external_session_id,
            content_hash=content_hash,
            session_path=session_path,
            linked_at=linked_at or datetime.now(UTC),
        )
        try:
            connection.execute(
                "INSERT INTO agent_sessions (session_link_id, attempt_id, agent, "
                "external_session_id, content_hash, linked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    link.session_link_id,
                    link.attempt_id,
                    link.agent,
                    link.external_session_id,
                    link.content_hash,
                    link.linked_at.isoformat(),
                ),
            )
            if session_path is not None:
                locator_owner = connection.execute(
                    "SELECT session_link_id FROM session_locators WHERE locator = ?",
                    (session_path,),
                ).fetchone()
                if locator_owner is not None:
                    raise ConflictingSessionLinkError(
                        "Session file path is already linked to another session"
                    )
                connection.execute(
                    "INSERT INTO session_locators (session_link_id, locator) VALUES (?, ?)",
                    (link.session_link_id, session_path),
                )
        except sqlite3.IntegrityError as exc:
            existing = connection.execute(
                "SELECT agent_sessions.*, session_locators.locator AS session_path "
                "FROM agent_sessions LEFT JOIN session_locators "
                "ON session_locators.session_link_id = agent_sessions.session_link_id "
                "WHERE agent = ? AND external_session_id = ?",
                (agent, external_session_id),
            ).fetchone()
            if existing is None:
                raise
            if existing["attempt_id"] != attempt_id or (
                content_hash is not None and existing["content_hash"] != content_hash
            ):
                raise ConflictingSessionLinkError(
                    "External session id is already linked to a different attempt"
                ) from exc
            existing_path = existing["session_path"]
            if existing_path is not None and existing_path != session_path:
                raise ConflictingSessionLinkError(
                    "External session is already linked from a different file path"
                ) from exc
            if existing_path is None and session_path is not None:
                locator_owner = connection.execute(
                    "SELECT session_link_id FROM session_locators WHERE locator = ?",
                    (session_path,),
                ).fetchone()
                if locator_owner is not None:
                    raise ConflictingSessionLinkError(
                        "Session file path is already linked to another session"
                    ) from exc
                connection.execute(
                    "INSERT INTO session_locators (session_link_id, locator) VALUES (?, ?)",
                    (existing["session_link_id"], session_path),
                )
                existing = connection.execute(
                    "SELECT agent_sessions.*, session_locators.locator AS session_path "
                    "FROM agent_sessions LEFT JOIN session_locators "
                    "ON session_locators.session_link_id = agent_sessions.session_link_id "
                    "WHERE agent_sessions.session_link_id = ?",
                    (existing["session_link_id"],),
                ).fetchone()
                if existing is None:
                    raise ConflictingSessionLinkError(
                        "External session link disappeared while updating its path"
                    ) from exc
            return OutcomeLedger._row_to_session_link(existing)
        return link

    def replace_unmeasured_session(
        self,
        task_id: str,
        *,
        attempt_id: str,
        agent: str,
        external_session_id: str,
        content_hash: str,
        session_path: str,
        expected_repository_id: str,
    ) -> tuple[ExecutionAttempt, AgentSessionLink]:
        """Explicitly refresh one unmeasured session, preserving its link identity.

        Refuses replacement after any persisted measurement: a scored transcript is immutable.
        The new validated session must retain its agent/session identity and attempt.
        """
        normalized_agent, normalized_session_id, normalized_hash, normalized_path = (
            _normalize_session_link(agent, external_session_id, content_hash, session_path)
        )
        if normalized_hash is None or normalized_path is None:
            raise ValueError("Replacement requires a content hash and session path")
        agent = normalized_agent
        external_session_id = normalized_session_id
        content_hash = normalized_hash
        session_path = normalized_path
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT agent_sessions.*, session_locators.locator AS session_path "
                    "FROM agent_sessions LEFT JOIN session_locators "
                    "ON session_locators.session_link_id = agent_sessions.session_link_id "
                    "WHERE agent_sessions.attempt_id = ?",
                    (attempt_id,),
                ).fetchall()
                if len(row) != 1:
                    raise ConflictingSessionLinkError(
                        "Replacement requires exactly one existing session on the attempt"
                    )
                old = row[0]
                attempt_row = connection.execute(
                    "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
                task = connection.execute(
                    "SELECT repository_id FROM outcomes WHERE task_id = ?", (task_id,)
                ).fetchone()
                if (
                    attempt_row is None
                    or task is None
                    or attempt_row["task_id"] != task_id
                    or task["repository_id"] != expected_repository_id
                ):
                    raise CrossRepositoryError("Session replacement task or repository mismatch")
                if old["agent"] != agent or old["external_session_id"] != external_session_id:
                    raise ConflictingSessionLinkError(
                        "Replacement cannot change the agent or external session identity"
                    )
                if (
                    connection.execute(
                        "SELECT 1 FROM ter_measurements WHERE attempt_id = ? "
                        "OR session_link_id = ? LIMIT 1",
                        (attempt_id, old["session_link_id"]),
                    ).fetchone()
                    is not None
                ):
                    raise ConflictingSessionLinkError(
                        "Measured sessions cannot be replaced; create a new attempt"
                    )
                owner = connection.execute(
                    "SELECT session_link_id FROM session_locators WHERE locator = ?",
                    (session_path,),
                ).fetchone()
                if owner is not None and owner["session_link_id"] != old["session_link_id"]:
                    raise ConflictingSessionLinkError("Session file path belongs to another link")
                connection.execute(
                    "UPDATE agent_sessions SET content_hash = ? WHERE session_link_id = ?",
                    (content_hash, old["session_link_id"]),
                )
                connection.execute(
                    "DELETE FROM session_locators WHERE session_link_id = ?",
                    (old["session_link_id"],),
                )
                connection.execute(
                    "INSERT INTO session_locators (session_link_id, locator) VALUES (?, ?)",
                    (old["session_link_id"], session_path),
                )
                refreshed = connection.execute(
                    "SELECT agent_sessions.*, session_locators.locator AS session_path "
                    "FROM agent_sessions LEFT JOIN session_locators "
                    "ON session_locators.session_link_id = agent_sessions.session_link_id "
                    "WHERE agent_sessions.session_link_id = ?",
                    (old["session_link_id"],),
                ).fetchone()
                result = (self._row_to_attempt(attempt_row), self._row_to_session_link(refreshed))
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def link_agent_session_for_task(
        self,
        task_id: str,
        *,
        agent: str,
        external_session_id: str,
        content_hash: str | None = None,
        session_path: str | None = None,
        expected_repository_id: str | None = None,
        attempt_id: str | None = None,
        started_at: datetime | None = None,
    ) -> tuple[ExecutionAttempt, AgentSessionLink, bool]:
        """Atomically select/create an attempt and link a validated agent session."""
        agent_name, session_id, normalized_hash, normalized_path = _normalize_session_link(
            agent, external_session_id, content_hash, session_path
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT repository_id FROM outcomes WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise KeyError(f"Unknown task id: {task_id}")
                repository_id = str(task["repository_id"])
                if expected_repository_id is not None and expected_repository_id != repository_id:
                    raise CrossRepositoryError(
                        "Execution attempt repository does not match the task repository"
                    )

                existing = connection.execute(
                    "SELECT agent_sessions.*, session_locators.locator AS session_path "
                    "FROM agent_sessions LEFT JOIN session_locators "
                    "ON session_locators.session_link_id = agent_sessions.session_link_id "
                    "WHERE agent = ? AND external_session_id = ?",
                    (agent_name, session_id),
                ).fetchone()
                if existing is not None:
                    attempt_row = connection.execute(
                        "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                        (existing["attempt_id"],),
                    ).fetchone()
                    if attempt_row is None or attempt_row["task_id"] != task_id:
                        raise ConflictingSessionLinkError(
                            "External session id is already linked to a different task"
                        )
                    if attempt_id is not None and attempt_row["attempt_id"] != attempt_id:
                        raise ConflictingSessionLinkError(
                            "External session id is already linked to a different execution attempt"
                        )
                    if existing["content_hash"] != normalized_hash:
                        raise ConflictingSessionLinkError(
                            "External session content changed after it was linked"
                        )
                    existing_path = existing["session_path"]
                    if existing_path is not None and existing_path != normalized_path:
                        raise ConflictingSessionLinkError(
                            "External session is already linked from a different file path"
                        )
                    if existing_path is None and normalized_path is not None:
                        connection.execute(
                            "INSERT INTO session_locators (session_link_id, locator) VALUES (?, ?)",
                            (existing["session_link_id"], normalized_path),
                        )
                        existing = connection.execute(
                            "SELECT agent_sessions.*, session_locators.locator AS session_path "
                            "FROM agent_sessions LEFT JOIN session_locators "
                            "ON session_locators.session_link_id = agent_sessions.session_link_id "
                            "WHERE agent_sessions.session_link_id = ?",
                            (existing["session_link_id"],),
                        ).fetchone()
                        if existing is None:
                            raise ConflictingSessionLinkError(
                                "External session link disappeared while updating its path"
                            )
                    attempt = self._row_to_attempt(attempt_row)
                    link = self._row_to_session_link(existing)
                    connection.commit()
                    return attempt, link, False

                attempt = self._select_or_create_attempt(
                    connection,
                    task_id,
                    expected_repository_id=repository_id,
                    attempt_id=attempt_id,
                    started_at=started_at,
                )
                if (
                    connection.execute(
                        "SELECT 1 FROM agent_sessions WHERE attempt_id = ? LIMIT 1",
                        (attempt.attempt_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ConflictingSessionLinkError(
                        "Execution attempt already has a linked session; create a retry attempt"
                    )
                link = self._insert_agent_session(
                    connection,
                    attempt_id=attempt.attempt_id,
                    agent=agent_name,
                    external_session_id=session_id,
                    content_hash=normalized_hash,
                    session_path=normalized_path,
                    linked_at=None,
                )
                connection.commit()
                return attempt, link, True
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _select_or_create_attempt(
        connection: sqlite3.Connection,
        task_id: str,
        *,
        expected_repository_id: str,
        attempt_id: str | None,
        started_at: datetime | None,
    ) -> ExecutionAttempt:
        if attempt_id is not None:
            attempt_row = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt_row is None:
                raise KeyError(f"Unknown execution attempt: {attempt_id}")
            if attempt_row["task_id"] != task_id:
                raise CorrelationMismatchError("Execution attempt belongs to a different task")
            return OutcomeLedger._row_to_attempt(attempt_row)

        latest = connection.execute(
            "SELECT * FROM execution_attempts WHERE task_id = ? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if latest is not None:
            session_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_sessions WHERE attempt_id = ?",
                    (latest["attempt_id"],),
                ).fetchone()[0]
            )
            if session_count == 0:
                return OutcomeLedger._row_to_attempt(latest)
        return OutcomeLedger._insert_execution_attempt(
            connection,
            task_id,
            expected_repository_id=expected_repository_id,
            started_at=started_at,
            status="pending",
        )

    def record_ter_measurement(
        self,
        attempt_id: str,
        *,
        analyzer: str,
        analyzer_version: str,
        metrics: Mapping[str, float | int | None] | None = None,
        session_link_id: str | None = None,
        measured_at: datetime | None = None,
    ) -> TerMeasurement:
        """Persist numeric TER-style measurements without storing TER internals."""
        return self.record_ter_measurements(
            [
                TerMeasurementRequest(
                    attempt_id=attempt_id,
                    analyzer=analyzer,
                    analyzer_version=analyzer_version,
                    metrics=metrics or {},
                    session_link_id=session_link_id,
                    measured_at=measured_at,
                )
            ]
        )[0]

    def record_ter_measurements(
        self, requests: Iterable[TerMeasurementRequest]
    ) -> list[TerMeasurement]:
        """Persist a batch of measurements atomically."""
        normalized_requests: list[TerMeasurementRequest] = []
        for request in requests:
            analyzer_name = request.analyzer.strip()
            version = request.analyzer_version.strip()
            if not analyzer_name:
                raise ValueError("Analyzer name must not be empty")
            if not version:
                raise ValueError("Analyzer version must not be empty")
            normalized_requests.append(
                TerMeasurementRequest(
                    attempt_id=request.attempt_id,
                    analyzer=analyzer_name,
                    analyzer_version=version,
                    metrics=self._normalize_metrics(request.metrics),
                    session_link_id=request.session_link_id,
                    measured_at=request.measured_at,
                )
            )
        if not normalized_requests:
            return []

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                measurements = [
                    self._record_ter_measurement(connection, request)
                    for request in normalized_requests
                ]
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return measurements

    def _record_ter_measurement(
        self, connection: sqlite3.Connection, request: TerMeasurementRequest
    ) -> TerMeasurement:
        attempt_id = request.attempt_id
        analyzer_name = request.analyzer
        version = request.analyzer_version
        session_link_id = request.session_link_id
        normalized_metrics = dict(request.metrics)
        metrics_json = json.dumps(normalized_metrics, sort_keys=True)
        attempt = connection.execute(
            "SELECT attempt_id FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise KeyError(f"Unknown attempt id: {attempt_id}")
        if session_link_id is not None:
            link = connection.execute(
                "SELECT attempt_id FROM agent_sessions WHERE session_link_id = ?",
                (session_link_id,),
            ).fetchone()
            if link is None:
                raise KeyError(f"Unknown session link id: {session_link_id}")
            if link["attempt_id"] != attempt_id:
                raise CorrelationMismatchError(
                    "Session link belongs to a different execution attempt"
                )
        measurement = TerMeasurement(
            measurement_id=str(uuid.uuid4()),
            attempt_id=attempt_id,
            session_link_id=session_link_id,
            analyzer=analyzer_name,
            analyzer_version=version,
            measured_at=request.measured_at or datetime.now(UTC),
            metrics=normalized_metrics,
        )
        try:
            connection.execute(
                "INSERT INTO ter_measurements (measurement_id, attempt_id, session_link_id, "
                "analyzer, analyzer_version, measured_at, metrics) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    measurement.measurement_id,
                    measurement.attempt_id,
                    measurement.session_link_id,
                    measurement.analyzer,
                    measurement.analyzer_version,
                    measurement.measured_at.isoformat(),
                    metrics_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            existing = connection.execute(
                "SELECT * FROM ter_measurements WHERE attempt_id = ? "
                "AND analyzer = ? AND analyzer_version = ?",
                (attempt_id, analyzer_name, version),
            ).fetchone()
            if existing is None:
                raise
            if (
                existing["session_link_id"] != session_link_id
                or existing["metrics"] != metrics_json
            ):
                raise ConflictingMeasurementError(
                    "A different measurement already exists for this attempt and analyzer"
                ) from exc
            return self._row_to_measurement(existing)
        return measurement

    @staticmethod
    def _normalize_metrics(
        metrics: Mapping[str, float | int | None],
    ) -> dict[str, float | int | None]:
        normalized: dict[str, float | int | None] = {}
        for key, value in metrics.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Metric names must be non-empty strings")
            if value is None:
                normalized[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Metric {key!r} must be a finite number or null")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"Metric {key!r} must be a finite number or null")
            normalized[key] = value

        ter_fields = _TER_METRIC_FIELDS.intersection(normalized)
        if not ter_fields:
            return dict(sorted(normalized.items()))

        required = {"aggregate_ter", "aligned_tokens", "waste_tokens"}
        missing = sorted(required - ter_fields)
        if missing:
            raise ValueError(
                "TER measurements must include aggregate_ter, aligned_tokens, and "
                "waste_tokens; missing: " + ", ".join(missing)
            )

        aligned = normalized["aligned_tokens"]
        waste = normalized["waste_tokens"]
        aggregate = normalized["aggregate_ter"]
        if isinstance(aligned, bool) or not isinstance(aligned, int) or aligned < 0:
            raise ValueError("TER metric 'aligned_tokens' must be a non-negative integer")
        if isinstance(waste, bool) or not isinstance(waste, int) or waste < 0:
            raise ValueError("TER metric 'waste_tokens' must be a non-negative integer")
        if (
            isinstance(aggregate, bool)
            or not isinstance(aggregate, (int, float))
            or (isinstance(aggregate, float) and not math.isfinite(aggregate))
            or not 0 <= aggregate <= 1
        ):
            raise ValueError("TER metric 'aggregate_ter' must be a finite value between 0 and 1")

        if "total_tokens" in normalized:
            total = normalized["total_tokens"]
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("TER metric 'total_tokens' must be a non-negative integer")
            if total != aligned + waste:
                raise ValueError("TER metric 'total_tokens' must equal aligned plus waste tokens")
        if aligned + waste == 0:
            raise ValueError("TER measurement must contain positive token evidence")
        return dict(sorted(normalized.items()))

    def execution_attempts(self, task_id: str) -> list[ExecutionAttempt]:
        """Return execution attempts recorded for a task, ordered by attempt number."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM execution_attempts WHERE task_id = ? ORDER BY attempt_number",
                (task_id,),
            ).fetchall()
        return self._decoded_rows(rows, self._read_attempt)

    def all_execution_attempts(self) -> list[ExecutionAttempt]:
        """Return every execution attempt, ordered by task and attempt number."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM execution_attempts ORDER BY task_id, attempt_number"
            ).fetchall()
        return self._decoded_rows(rows, self._read_attempt)

    def agent_sessions(self, attempt_id: str) -> list[AgentSessionLink]:
        """Return external agent-session links recorded for an attempt."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT agent_sessions.*, session_locators.locator AS session_path "
                "FROM agent_sessions LEFT JOIN session_locators "
                "ON session_locators.session_link_id = agent_sessions.session_link_id "
                "WHERE agent_sessions.attempt_id = ? "
                "ORDER BY linked_at, session_link_id",
                (attempt_id,),
            ).fetchall()
        return self._decoded_rows(rows, self._read_session_link)

    def ter_measurements(self, attempt_id: str) -> list[TerMeasurement]:
        """Return TER-style measurements recorded for an attempt."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM ter_measurements WHERE attempt_id = ? "
                "ORDER BY measured_at, measurement_id",
                (attempt_id,),
            ).fetchall()
        return self._decoded_rows(rows, self._read_measurement)

    def _update(self, task_id: str, **values: Any) -> None:
        if not values:
            return
        columns = ", ".join(f"{name} = ?" for name in values)
        normalized = [self._sql_value(value) for value in values.values()]
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"UPDATE outcomes SET {columns} WHERE task_id = ?",
                (*normalized, task_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown task id: {task_id}")

    @staticmethod
    def _sql_value(value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        return value

    def query(
        self,
        *,
        category: TaskCategory | None = None,
        model: str | None = None,
        repository_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[OutcomeTask]:
        clauses: list[str] = []
        values: list[Any] = []
        if category is not None:
            clauses.append("task_category = ?")
            values.append(category.value)
        if model is not None:
            clauses.append("selected_model = ?")
            values.append(model)
        if repository_id is not None:
            clauses.append("repository_id = ?")
            values.append(repository_id)
        if since is not None:
            clauses.append("timestamp >= ?")
            values.append(since.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"SELECT * FROM outcomes{where} ORDER BY timestamp DESC, task_id LIMIT ?",
                (*values, limit),
            ).fetchall()
        return self._decoded_rows(rows, self._read_task)

    def _decoded_rows(
        self, rows: Iterable[Any], decoder: Callable[[Any], _RowT | None]
    ) -> list[_RowT]:
        decoded = [decoder(row) for row in rows]
        return [value for value in decoded if value is not None]

    def _quarantine(self, row: Any, kind: str) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            raise LedgerReadError(
                f"Outcome ledger contains an unreadable {kind} record; "
                "the record cannot be interpreted safely"
            ) from None
        if kind == "task":
            diagnostics.excluded_tasks += 1
            task_id = _row_value(row, "task_id")
            if isinstance(task_id, str):
                diagnostics.quarantined_task_ids.add(task_id)
        elif kind == "attempt":
            diagnostics.excluded_attempts += 1
        elif kind == "session":
            diagnostics.excluded_sessions += 1
            session_link_id = _row_value(row, "session_link_id")
            if isinstance(session_link_id, str):
                diagnostics.quarantined_session_link_ids.add(session_link_id)
        else:
            diagnostics.excluded_measurements += 1

    def _read_task(self, row: Any) -> OutcomeTask | None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            try:
                return self._row_to_model(row)
            except _ROW_DECODE_ERRORS:
                self._quarantine(row, "task")
                return None
        data = dict(row)
        if safe_json_object(data.get("metadata")) is None:
            diagnostics.invalid_metadata_tasks += 1
            data["metadata"] = "{}"
        if data.get("routing_confidence") is not None:
            confidence = _coerce_finite_number(data["routing_confidence"])
            if confidence is None:
                diagnostics.invalid_confidence_tasks += 1
            data["routing_confidence"] = confidence
        try:
            return self._row_to_model(data)
        except _ROW_DECODE_ERRORS:
            self._quarantine(row, "task")
            return None

    def _read_attempt(self, row: Any) -> ExecutionAttempt | None:
        try:
            return self._row_to_attempt(row)
        except _ROW_DECODE_ERRORS:
            self._quarantine(row, "attempt")
            return None

    def _read_session_link(self, row: Any) -> AgentSessionLink | None:
        try:
            return self._row_to_session_link(row)
        except _ROW_DECODE_ERRORS:
            self._quarantine(row, "session")
            return None

    def _read_measurement(self, row: Any) -> TerMeasurement | None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            try:
                return self._row_to_measurement(row)
            except _ROW_DECODE_ERRORS:
                self._quarantine(row, "measurement")
                return None
        data = dict(row)
        if safe_json_object(data.get("metrics")) is None:
            diagnostics.invalid_measurement_metrics += 1
            data["metrics"] = "{}"
        try:
            return self._row_to_measurement(data)
        except _ROW_DECODE_ERRORS:
            self._quarantine(row, "measurement")
            return None

    def export_json(self, records: Iterable[OutcomeTask] | None = None) -> str:
        selected = list(records) if records is not None else self.query(limit=10000)
        return json.dumps(
            [record.model_dump(mode="json") for record in selected], indent=2, sort_keys=True
        )

    def confidence_records(self, repository_id: str) -> list[dict[str, Any]]:
        """Read one raw projection per task without coercing historical confidence.

        Always use the existing read-only connection, even on a writable ledger object.
        No attempts, sessions, descriptions, retrieved paths, or routing reasons are read.
        """
        with closing(self._connect_read_only()) as connection:
            rows = connection.execute(
                "SELECT task_id, timestamp, repository_id, request_hash, task_category, "
                "routing_confidence, selected_model, selected_capability_tier, "
                "execution_status, tests_passed, static_checks_passed, human_accepted, "
                "rework_required, safety_violation, metadata "
                "FROM outcomes WHERE repository_id = ? ORDER BY task_id",
                (repository_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, records: Iterable[OutcomeTask] | None = None) -> str:
        selected = list(records) if records is not None else self.query(limit=10000)
        buffer = io.StringIO()
        if not selected:
            return ""
        rows = [record.model_dump(mode="json") for record in selected]
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )
        return buffer.getvalue()

    @staticmethod
    def _row_to_model(row: Any) -> OutcomeTask:
        def optional_bool(name: str) -> bool | None:
            value = row[name]
            return None if value is None else bool(value)

        return OutcomeTask(
            task_id=row["task_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            repository_id=row["repository_id"],
            request_hash=row["request_hash"],
            task_category=TaskCategory(row["task_category"]),
            initial_complexity_score=row["initial_complexity_score"],
            revised_complexity_score=row["revised_complexity_score"],
            routing_confidence=row["routing_confidence"],
            selected_model=row["selected_model"],
            selected_capability_tier=(
                CapabilityTier(row["selected_capability_tier"])
                if row["selected_capability_tier"]
                else None
            ),
            routing_reasons=json.loads(row["routing_reasons"]),
            retrieved_files=json.loads(row["retrieved_files"]),
            retrieved_symbols=json.loads(row["retrieved_symbols"]),
            context_token_estimate=row["context_token_estimate"],
            execution_status=row["execution_status"],
            tests_passed=optional_bool("tests_passed"),
            static_checks_passed=optional_bool("static_checks_passed"),
            human_accepted=optional_bool("human_accepted"),
            rework_required=optional_bool("rework_required"),
            retry_count=row["retry_count"],
            latency_seconds=row["latency_seconds"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            estimated_cost=row["estimated_cost"],
            failure_category=row["failure_category"],
            safety_violation=optional_bool("safety_violation"),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_attempt(row: Any) -> ExecutionAttempt:
        return ExecutionAttempt(
            attempt_id=row["attempt_id"],
            task_id=row["task_id"],
            repository_id=row["repository_id"],
            routing_decision_id=row["routing_decision_id"],
            attempt_number=row["attempt_number"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
            status=row["status"],
        )

    @staticmethod
    def _row_to_session_link(row: Any) -> AgentSessionLink:
        return AgentSessionLink(
            session_link_id=row["session_link_id"],
            attempt_id=row["attempt_id"],
            agent=row["agent"],
            external_session_id=row["external_session_id"],
            content_hash=row["content_hash"],
            session_path=row["session_path"],
            linked_at=datetime.fromisoformat(row["linked_at"]),
        )

    @staticmethod
    def _row_to_measurement(row: Any) -> TerMeasurement:
        return TerMeasurement(
            measurement_id=row["measurement_id"],
            attempt_id=row["attempt_id"],
            session_link_id=row["session_link_id"],
            analyzer=row["analyzer"],
            analyzer_version=row["analyzer_version"],
            measured_at=datetime.fromisoformat(row["measured_at"]),
            metrics=json.loads(row["metrics"]),
        )
