"""Explicit, privacy-minimal linking of external agent sessions to TGV outcomes."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tgv.exceptions import (
    ConflictingSessionLinkError,
    CrossRepositoryError,
    OutcomeCorrelationError,
    ValidationFailure,
)
from tgv.models import AgentSessionLink, ExecutionAttempt
from tgv.outcomes import OutcomeLedger

MAX_SESSION_BYTES = 128 * 1024 * 1024
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_CLAUDE_MESSAGE_TYPES = frozenset({"user", "assistant"})


@dataclass(frozen=True)
class ValidatedSession:
    """Validated session metadata; deliberately excludes the file path and contents."""

    agent: str
    external_session_id: str
    content_hash: str


@dataclass(frozen=True)
class SessionLinkResult:
    """Outcome of an explicit session-link request."""

    task_id: str
    attempt: ExecutionAttempt
    link: AgentSessionLink
    created: bool


def supported_agents() -> tuple[str, ...]:
    """Return the agent-session formats TGV can validate deterministically."""
    return tuple(sorted(_AGENT_VALIDATORS))


def validate_agent_session(path: Path, agent: str) -> ValidatedSession:
    """Validate a supported session file without retaining its path or contents."""
    normalized = agent.strip().lower()
    validator = _AGENT_VALIDATORS.get(normalized)
    if validator is None:
        supported = ", ".join(supported_agents())
        raise ValidationFailure(
            f"Unsupported agent session format {agent!r}; supported formats: {supported}"
        )
    session_path = _validated_session_path(path)
    session_id, content_hash = validator(session_path)
    if not _SESSION_ID.fullmatch(session_id):
        raise ValidationFailure(
            "Agent session id must be a bounded token using letters, digits, "
            "'.', '_', ':', '@', '+', or '-'"
        )
    return ValidatedSession(
        agent=normalized,
        external_session_id=session_id,
        content_hash=content_hash,
    )


def ensure_task_repository(ledger: OutcomeLedger, task_id: str, repository_root: Path) -> str:
    """Validate that a task exists and belongs to the requested repository."""
    try:
        task_repository_id = ledger.task_repository_id(task_id)
    except KeyError as exc:
        raise OutcomeCorrelationError(f"Unknown task id: {task_id}") from exc
    expected_repository_id = ledger.repository_id(repository_root)
    if task_repository_id != expected_repository_id:
        raise CrossRepositoryError("Task repository does not match the current repository")
    return expected_repository_id


def link_session_to_task(
    ledger: OutcomeLedger,
    task_id: str,
    *,
    repository_root: Path,
    session_path: Path,
    agent: str,
    attempt_id: str | None = None,
) -> SessionLinkResult:
    """Link a validated external session to an attempt for an existing task."""
    validated = validate_agent_session(session_path, agent)
    repository_id = ensure_task_repository(ledger, task_id, repository_root)

    existing = ledger.agent_session(validated.agent, validated.external_session_id)
    if existing is not None:
        attempts = ledger.execution_attempts(task_id)
        attempt = next((item for item in attempts if item.attempt_id == existing.attempt_id), None)
        if attempt is None:
            raise ConflictingSessionLinkError(
                "External session id is already linked to a different task"
            )
        if attempt_id is not None and attempt.attempt_id != attempt_id:
            raise ConflictingSessionLinkError(
                "External session id is already linked to a different execution attempt"
            )
        if existing.content_hash != validated.content_hash:
            raise ConflictingSessionLinkError(
                "External session content changed after it was linked"
            )
        return SessionLinkResult(task_id=task_id, attempt=attempt, link=existing, created=False)

    attempts = ledger.execution_attempts(task_id)
    if attempt_id is not None:
        attempt = next((item for item in attempts if item.attempt_id == attempt_id), None)
        if attempt is None:
            raise OutcomeCorrelationError(
                f"Unknown execution attempt {attempt_id!r} for task {task_id}"
            )
    elif attempts and not ledger.agent_sessions(attempts[-1].attempt_id):
        attempt = attempts[-1]
    else:
        attempt = ledger.create_execution_attempt(task_id, expected_repository_id=repository_id)

    link = ledger.link_agent_session(
        attempt.attempt_id,
        agent=validated.agent,
        external_session_id=validated.external_session_id,
        content_hash=validated.content_hash,
    )
    return SessionLinkResult(task_id=task_id, attempt=attempt, link=link, created=True)


def _validated_session_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        info = expanded.lstat()
    except (OSError, ValueError) as exc:
        raise ValidationFailure(f"Agent session file does not exist: {expanded}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValidationFailure("Agent session path must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise ValidationFailure("Agent session path must be a regular file")
    if info.st_size > MAX_SESSION_BYTES:
        raise ValidationFailure(
            f"Agent session file exceeds the {MAX_SESSION_BYTES}-byte safety limit"
        )
    try:
        return expanded.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationFailure("Agent session file is not readable") from exc


def _validate_claude_code(path: Path) -> tuple[str, str]:
    if path.suffix.lower() != ".jsonl":
        raise ValidationFailure("Claude Code sessions must be .jsonl files")

    digest = hashlib.sha256()
    session_id: str | None = None
    saw_user = False
    saw_assistant = False
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                digest.update(raw_line)
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ValidationFailure(
                        f"Invalid JSON in agent session at line {line_number}"
                    ) from exc
                if not isinstance(entry, dict):
                    raise ValidationFailure("Agent session records must be JSON objects")
                entry_type = entry.get("type")
                if not isinstance(entry_type, str) or entry_type not in _CLAUDE_MESSAGE_TYPES:
                    continue
                if not isinstance(entry.get("message"), dict):
                    raise ValidationFailure(
                        f"Agent session record at line {line_number} has no message object"
                    )
                candidate = entry.get("sessionId")
                if not isinstance(candidate, str) or not candidate.strip():
                    raise ValidationFailure(
                        f"Agent session record at line {line_number} is missing sessionId"
                    )
                candidate_id = candidate.strip()
                if session_id is None:
                    session_id = candidate_id
                elif candidate_id != session_id:
                    raise ValidationFailure("Agent session contains mismatched sessionId values")
                saw_user = saw_user or entry_type == "user"
                saw_assistant = saw_assistant or entry_type == "assistant"
    except OSError as exc:
        raise ValidationFailure("Agent session file is not readable") from exc

    if session_id is None:
        raise ValidationFailure("Agent session does not contain a sessionId")
    if not (saw_user and saw_assistant):
        raise ValidationFailure(
            "Agent session must contain at least one user and one assistant message"
        )
    return session_id, digest.hexdigest()


_AGENT_VALIDATORS: dict[str, Callable[[Path], tuple[str, str]]] = {
    "claude-code": _validate_claude_code,
}
