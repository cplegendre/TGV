"""Explicit, privacy-minimal linking of external agent sessions to TGV outcomes."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from tgv.exceptions import (
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
    """Validated session metadata and the exact bytes that were validated."""

    agent: str
    external_session_id: str
    content_hash: str
    session_path: Path
    content: bytes = field(repr=False)


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
    """Validate a supported session file and return its exact validated bytes."""
    normalized = agent.strip().lower()
    validator = _AGENT_VALIDATORS.get(normalized)
    if validator is None:
        supported = ", ".join(supported_agents())
        raise ValidationFailure(
            f"Unsupported agent session format {agent!r}; supported formats: {supported}"
        )
    session_id, content_hash, content = validator(path.expanduser())
    if not _SESSION_ID.fullmatch(session_id):
        raise ValidationFailure(
            "Agent session id must be a bounded token using letters, digits, "
            "'.', '_', ':', '@', '+', or '-'"
        )
    return ValidatedSession(
        agent=normalized,
        external_session_id=session_id,
        content_hash=content_hash,
        session_path=path.expanduser().absolute(),
        content=content,
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
    replace_unmeasured: bool = False,
) -> SessionLinkResult:
    """Link a validated external session to an attempt for an existing task."""
    validated = validate_agent_session(session_path, agent)
    if replace_unmeasured:
        if attempt_id is None:
            raise ValidationFailure("--replace-unmeasured requires --attempt")
        attempt, link = ledger.replace_unmeasured_session(
            task_id,
            attempt_id=attempt_id,
            agent=validated.agent,
            external_session_id=validated.external_session_id,
            content_hash=validated.content_hash,
            session_path=str(validated.session_path),
            expected_repository_id=ledger.repository_id(repository_root),
        )
        return SessionLinkResult(task_id=task_id, attempt=attempt, link=link, created=False)
    try:
        attempt, link, created = ledger.link_agent_session_for_task(
            task_id,
            agent=validated.agent,
            external_session_id=validated.external_session_id,
            content_hash=validated.content_hash,
            session_path=str(validated.session_path),
            expected_repository_id=ledger.repository_id(repository_root),
            attempt_id=attempt_id,
        )
    except KeyError as exc:
        raise OutcomeCorrelationError(str(exc)) from exc
    return SessionLinkResult(
        task_id=task_id,
        attempt=attempt,
        link=link,
        created=created,
    )


def _open_session_file(path: Path) -> BinaryIO:
    """Open a session file without following a substituted final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    if nofollow == 0:
        try:
            if path.is_symlink():
                raise ValidationFailure("Agent session path must not be a symbolic link")
        except OSError as exc:
            raise ValidationFailure(f"Agent session file does not exist: {path}") from exc
    try:
        descriptor = os.open(path, flags)
    except ValueError as exc:
        raise ValidationFailure("Agent session path is invalid") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValidationFailure("Agent session path must not be a symbolic link") from exc
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise ValidationFailure(f"Agent session file does not exist: {path}") from exc
        raise ValidationFailure(f"Agent session file is not readable: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if stat.S_ISLNK(info.st_mode):
            raise ValidationFailure("Agent session path must not be a symbolic link")
        if not stat.S_ISREG(info.st_mode):
            raise ValidationFailure("Agent session path must be a regular file")
        if info.st_size > MAX_SESSION_BYTES:
            raise ValidationFailure(
                f"Agent session file exceeds the {MAX_SESSION_BYTES}-byte safety limit"
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _validate_claude_code(path: Path) -> tuple[str, str, bytes]:
    if path.suffix.lower() != ".jsonl":
        raise ValidationFailure("Claude Code sessions must be .jsonl files")

    digest = hashlib.sha256()
    session_id: str | None = None
    saw_user = False
    saw_assistant = False
    meaningful_user = False
    meaningful_assistant = False
    content = bytearray()
    try:
        with _open_session_file(path) as handle:
            line_number = 0
            total_bytes = 0
            while raw_line := handle.readline(MAX_SESSION_BYTES - total_bytes + 1):
                line_number += 1
                total_bytes += len(raw_line)
                if total_bytes > MAX_SESSION_BYTES:
                    raise ValidationFailure(
                        f"Agent session file grew beyond the {MAX_SESSION_BYTES}-byte safety limit"
                    )
                digest.update(raw_line)
                content.extend(raw_line)
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
                message_content = entry["message"].get("content")
                # Claude exports text either directly or as text blocks. Tool-only
                # traffic is not sufficient proof of a scorable conversation.
                meaningful = (
                    isinstance(message_content, str) and bool(message_content.strip())
                ) or (
                    isinstance(message_content, list)
                    and any(
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                        and bool(block["text"].strip())
                        for block in message_content
                    )
                )
                if entry_type == "user":
                    meaningful_user = meaningful_user or meaningful
                else:
                    meaningful_assistant = meaningful_assistant or meaningful
    except OSError as exc:
        raise ValidationFailure("Agent session file is not readable") from exc

    if session_id is None:
        raise ValidationFailure("Agent session does not contain a sessionId")
    if not (saw_user and saw_assistant):
        raise ValidationFailure(
            "Agent session must contain at least one user and one assistant message"
        )
    if not (meaningful_user and meaningful_assistant):
        raise ValidationFailure("Agent session requires non-empty user and assistant text content")
    return session_id, digest.hexdigest(), bytes(content)


_AGENT_VALIDATORS: dict[str, Callable[[Path], tuple[str, str, bytes]]] = {
    "claude-code": _validate_claude_code,
}


def _validate_codex(path: Path) -> tuple[str, str, bytes]:
    """Validate the observed Codex rollout envelope, without scoring it.

    This is deliberately fail-closed: only session_meta and meaningful text
    records establish identity/content. TER must independently support Codex.
    """
    if path.suffix.lower() != ".jsonl" or not path.name.startswith("rollout-"):
        raise ValidationFailure("Codex sessions must be rollout-*.jsonl files")
    digest = hashlib.sha256()
    content = bytearray()
    session_id: str | None = None
    saw_user = False
    saw_assistant = False
    try:
        with _open_session_file(path) as handle:
            total_bytes = 0
            line_number = 0
            while raw := handle.readline(MAX_SESSION_BYTES - total_bytes + 1):
                line_number += 1
                total_bytes += len(raw)
                if total_bytes > MAX_SESSION_BYTES:
                    raise ValidationFailure("Codex session exceeds maximum file size")
                digest.update(raw)
                content.extend(raw)
                if not raw.strip():
                    continue
                try:
                    item = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationFailure(f"Invalid Codex JSON at line {line_number}") from exc
                if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                    raise ValidationFailure("Codex records require type and payload objects")
                kind = item.get("type")
                data = item["payload"]
                if kind == "session_meta":
                    candidate = data.get("session_id", data.get("id"))
                    if not isinstance(candidate, str) or not candidate.strip():
                        raise ValidationFailure("Codex session_meta is missing session identity")
                    candidate = candidate.strip()
                    if session_id is None:
                        session_id = candidate
                    elif session_id != candidate:
                        raise ValidationFailure("Conflicting Codex session identities")
                elif kind == "event_msg" and data.get("type") == "user_message":
                    saw_user |= isinstance(data.get("message"), str) and bool(
                        data["message"].strip()
                    )
                elif kind == "event_msg" and data.get("type") == "agent_message":
                    saw_assistant |= isinstance(data.get("message"), str) and bool(
                        data["message"].strip()
                    )
                elif kind == "response_item" and data.get("type") == "message":
                    role = data.get("role")
                    blocks = data.get("content")
                    meaningful = isinstance(blocks, list) and any(
                        isinstance(block, dict)
                        and block.get("type") in {"input_text", "output_text", "text"}
                        and isinstance(block.get("text"), str)
                        and bool(block["text"].strip())
                        for block in blocks
                    )
                    if role == "user":
                        saw_user |= meaningful
                    elif role == "assistant":
                        saw_assistant |= meaningful
    except OSError as exc:
        raise ValidationFailure("Codex session file is not readable") from exc
    if session_id is None:
        raise ValidationFailure("Codex rollout requires session_meta identity")
    if not (saw_user and saw_assistant):
        raise ValidationFailure("Codex rollout requires meaningful user and assistant text")
    return session_id, digest.hexdigest(), bytes(content)


_AGENT_VALIDATORS["codex"] = _validate_codex
