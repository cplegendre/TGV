from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tgv.models import CapabilityTier, OutcomeTask, TaskCategory, TaskProfile

_SCHEMA_VERSION = 1


class OutcomeLedger:
    """Privacy-minimal SQLite outcome store with transactional schema initialization."""

    def __init__(self, path: Path, *, retain_raw_prompts: bool = False) -> None:
        self.path = path
        self.retain_raw_prompts = retain_raw_prompts

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
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
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (_SCHEMA_VERSION,)
                )
            elif int(row[0]) != _SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported outcome schema version: {row[0]}")
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
