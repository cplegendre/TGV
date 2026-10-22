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
from collections.abc import Iterable, Mapping
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tgv.exceptions import (
    ConflictingMeasurementError,
    ConflictingSessionLinkError,
    CorrelationMismatchError,
    CrossRepositoryError,
)
from tgv.models import (
    AgentSessionLink,
    CapabilityTier,
    ExecutionAttempt,
    OutcomeTask,
    TaskCategory,
    TaskProfile,
    TerMeasurement,
)

_SCHEMA_VERSION = 2
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


def _normalize_session_link(
    agent: str, external_session_id: str, content_hash: str | None
) -> tuple[str, str, str | None]:
    agent_name = agent.strip()
    session_id = external_session_id.strip()
    if not agent_name:
        raise ValueError("Agent name must not be empty")
    if not session_id:
        raise ValueError("External session id must not be empty")
    normalized_hash = None if content_hash is None else content_hash.lower()
    if normalized_hash is not None and not _CONTENT_HASH.fullmatch(normalized_hash):
        raise ValueError("content_hash must be a sha256 hex digest")
    return agent_name, session_id, normalized_hash


class OutcomeLedger:
    """Privacy-minimal SQLite outcome store with transactional schema initialization."""

    def __init__(self, path: Path, *, retain_raw_prompts: bool = False) -> None:
        self.path = path
        self.retain_raw_prompts = retain_raw_prompts

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        with suppress(OSError):
            os.chmod(self.path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        self._initialize(connection)
        return connection

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
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (_SCHEMA_VERSION,)
                )
                return
            version = int(row[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported outcome schema version: {version}")
            if version == 1:
                OutcomeLedger._migrate_v1_to_v2(connection)
                connection.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
                return
            OutcomeLedger._create_v1_schema(connection)
            OutcomeLedger._create_v2_schema(connection)

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
    ) -> None:
        self.assign_routing_decision_id(task_id)
        self._update(
            task_id,
            revised_complexity_score=revised_profile.complexity_score,
            routing_confidence=confidence,
            selected_model=model,
            selected_capability_tier=capability_tier.value,
            routing_reasons=json.dumps(reasons),
            retrieved_files=json.dumps(files),
            retrieved_symbols=json.dumps(symbols),
            context_token_estimate=context_token_estimate,
        )

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
                "SELECT * FROM agent_sessions WHERE agent = ? AND external_session_id = ?",
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
        if not status.strip():
            raise ValueError("Execution attempt status must not be empty")
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

    def update_execution_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        completed_at: datetime | None = None,
    ) -> None:
        """Record a new status for an existing execution attempt."""
        if not status.strip():
            raise ValueError("Execution attempt status must not be empty")
        with closing(self._connect()) as connection, connection:
            if completed_at is None:
                cursor = connection.execute(
                    "UPDATE execution_attempts SET status = ? WHERE attempt_id = ?",
                    (status.strip(), attempt_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE execution_attempts SET status = ?, completed_at = ? "
                    "WHERE attempt_id = ?",
                    (status.strip(), completed_at.isoformat(), attempt_id),
                )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown attempt id: {attempt_id}")

    def link_agent_session(
        self,
        attempt_id: str,
        *,
        agent: str,
        external_session_id: str,
        content_hash: str | None = None,
        linked_at: datetime | None = None,
    ) -> AgentSessionLink:
        """Link an opaque external agent-session reference to an execution attempt."""
        agent_name, session_id, normalized_hash = _normalize_session_link(
            agent, external_session_id, content_hash
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
        linked_at: datetime | None,
    ) -> AgentSessionLink:
        link = AgentSessionLink(
            session_link_id=str(uuid.uuid4()),
            attempt_id=attempt_id,
            agent=agent,
            external_session_id=external_session_id,
            content_hash=content_hash,
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
        except sqlite3.IntegrityError as exc:
            existing = connection.execute(
                "SELECT * FROM agent_sessions WHERE agent = ? AND external_session_id = ?",
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
            return OutcomeLedger._row_to_session_link(existing)
        return link

    def link_agent_session_for_task(
        self,
        task_id: str,
        *,
        agent: str,
        external_session_id: str,
        content_hash: str | None = None,
        expected_repository_id: str | None = None,
        attempt_id: str | None = None,
        started_at: datetime | None = None,
    ) -> tuple[ExecutionAttempt, AgentSessionLink, bool]:
        """Atomically select/create an attempt and link a validated agent session."""
        agent_name, session_id, normalized_hash = _normalize_session_link(
            agent, external_session_id, content_hash
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
                    "SELECT * FROM agent_sessions WHERE agent = ? AND external_session_id = ?",
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
                link = self._insert_agent_session(
                    connection,
                    attempt_id=attempt.attempt_id,
                    agent=agent_name,
                    external_session_id=session_id,
                    content_hash=normalized_hash,
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
        analyzer_name = analyzer.strip()
        version = analyzer_version.strip()
        if not analyzer_name:
            raise ValueError("Analyzer name must not be empty")
        if not version:
            raise ValueError("Analyzer version must not be empty")
        normalized_metrics = self._normalize_metrics(metrics or {})
        metrics_json = json.dumps(normalized_metrics, sort_keys=True)
        with closing(self._connect()) as connection, connection:
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
                measured_at=measured_at or datetime.now(UTC),
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
        return dict(sorted(normalized.items()))

    def execution_attempts(self, task_id: str) -> list[ExecutionAttempt]:
        """Return execution attempts recorded for a task, ordered by attempt number."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM execution_attempts WHERE task_id = ? ORDER BY attempt_number",
                (task_id,),
            ).fetchall()
        return [self._row_to_attempt(row) for row in rows]

    def agent_sessions(self, attempt_id: str) -> list[AgentSessionLink]:
        """Return external agent-session links recorded for an attempt."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM agent_sessions WHERE attempt_id = ? "
                "ORDER BY linked_at, session_link_id",
                (attempt_id,),
            ).fetchall()
        return [self._row_to_session_link(row) for row in rows]

    def ter_measurements(self, attempt_id: str) -> list[TerMeasurement]:
        """Return TER-style measurements recorded for an attempt."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM ter_measurements WHERE attempt_id = ? "
                "ORDER BY measured_at, measurement_id",
                (attempt_id,),
            ).fetchall()
        return [self._row_to_measurement(row) for row in rows]

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
        return [self._row_to_model(row) for row in rows]

    def export_json(self, records: Iterable[OutcomeTask] | None = None) -> str:
        selected = list(records) if records is not None else self.query(limit=10000)
        return json.dumps(
            [record.model_dump(mode="json") for record in selected], indent=2, sort_keys=True
        )

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
    def _row_to_model(row: sqlite3.Row) -> OutcomeTask:
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
    def _row_to_attempt(row: sqlite3.Row) -> ExecutionAttempt:
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
    def _row_to_session_link(row: sqlite3.Row) -> AgentSessionLink:
        return AgentSessionLink(
            session_link_id=row["session_link_id"],
            attempt_id=row["attempt_id"],
            agent=row["agent"],
            external_session_id=row["external_session_id"],
            content_hash=row["content_hash"],
            linked_at=datetime.fromisoformat(row["linked_at"]),
        )

    @staticmethod
    def _row_to_measurement(row: sqlite3.Row) -> TerMeasurement:
        return TerMeasurement(
            measurement_id=row["measurement_id"],
            attempt_id=row["attempt_id"],
            session_link_id=row["session_link_id"],
            analyzer=row["analyzer"],
            analyzer_version=row["analyzer_version"],
            measured_at=datetime.fromisoformat(row["measured_at"]),
            metrics=json.loads(row["metrics"]),
        )
