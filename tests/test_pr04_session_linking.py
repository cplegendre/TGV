from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from tgv import session_linking
from tgv.cli import main
from tgv.config import default_config
from tgv.evaluation import run_outcome_benchmark
from tgv.exceptions import CorrelationMismatchError, CrossRepositoryError
from tgv.intelligence import TaskProfiler
from tgv.outcomes import OutcomeLedger
from tgv.session_linking import validate_agent_session

SESSION_ID = "8f3c4e5a-2b7d-4c1e-9a6f-1d2e3f4a5b6c"
OTHER_SESSION_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


def _session_lines(session_id: str = SESSION_ID) -> list[dict[str, object]]:
    return [
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": "2026-10-01T10:00:00.000Z",
            "message": {"role": "user", "content": "Fix the helper bug"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "parentUuid": "user-1",
            "sessionId": session_id,
            "requestId": "request-1",
            "timestamp": "2026-10-01T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "I fixed the helper."}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]


def _write_session(
    path: Path,
    *,
    session_id: str = SESSION_ID,
    lines: list[dict[str, object]] | None = None,
) -> Path:
    records = _session_lines(session_id) if lines is None else lines
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n\n", encoding="utf-8")
    return path


def _create_task(repo: Path, request: str = "fix helper bug") -> str:
    ledger = OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")
    profile = TaskProfiler(default_config().intelligence).profile(request)
    return ledger.create_pending(request, repo, profile)


def _ledger(repo: Path) -> OutcomeLedger:
    return OutcomeLedger(repo / ".tgv" / "outcomes.sqlite3")


def _shared_ledger_config(tmp_path: Path, repo: Path) -> Path:
    config = tmp_path / "shared-ledger.yaml"
    config.write_text(
        "models:\n"
        "  fast:\n"
        "    provider: external\n"
        "    model: tiny\n"
        "intelligence:\n"
        f"  outcome_database_path: {repo / '.tgv' / 'outcomes.sqlite3'}\n",
        encoding="utf-8",
    )
    return config


def _link(
    repo: Path,
    task_id: str,
    session: Path,
    *,
    agent: str = "claude-code",
    attempt_id: str | None = None,
    config: Path | None = None,
) -> list[str]:
    arguments = [
        "outcomes",
        "link",
        task_id,
        "--session",
        str(session),
        "--agent",
        agent,
        "--repo",
        str(repo),
    ]
    if attempt_id is not None:
        arguments.extend(["--attempt", attempt_id])
    if config is not None:
        arguments.extend(["--config", str(config)])
    return arguments


def _expect_exit(arguments: list[str], code: int, capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == code
    return capsys.readouterr().err


def test_link_records_validated_session_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")

    main(_link(repo, task_id, session))
    payload = json.loads(capsys.readouterr().out)

    assert payload["task_id"] == task_id
    assert payload["agent"] == "claude-code"
    assert payload["external_session_id"] == SESSION_ID
    assert payload["attempt_number"] == 1
    assert payload["created"] is True
    assert payload["content_hash"] == hashlib.sha256(session.read_bytes()).hexdigest()

    ledger = _ledger(repo)
    attempts = ledger.execution_attempts(task_id)
    assert [attempt.attempt_id for attempt in attempts] == [payload["attempt_id"]]
    links = ledger.agent_sessions(payload["attempt_id"])
    assert len(links) == 1
    assert links[0].session_link_id == payload["session_link_id"]
    assert links[0].content_hash == payload["content_hash"]


def test_link_is_idempotent_for_identical_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")

    main(_link(repo, task_id, session))
    first = json.loads(capsys.readouterr().out)
    main(_link(repo, task_id, session))
    second = json.loads(capsys.readouterr().out)

    assert second["created"] is False
    assert second["session_link_id"] == first["session_link_id"]
    assert second["attempt_id"] == first["attempt_id"]
    assert len(_ledger(repo).execution_attempts(task_id)) == 1


def test_link_allocates_a_new_attempt_for_a_new_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    first = _write_session(tmp_path / "first.jsonl")
    second = _write_session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID)

    main(_link(repo, task_id, first))
    first_payload = json.loads(capsys.readouterr().out)
    main(_link(repo, task_id, second))
    second_payload = json.loads(capsys.readouterr().out)

    assert first_payload["attempt_number"] == 1
    assert second_payload["attempt_number"] == 2
    assert first_payload["attempt_id"] != second_payload["attempt_id"]
    assert len(_ledger(repo).execution_attempts(task_id)) == 2


def test_link_targets_an_explicit_execution_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    ledger = _ledger(repo)
    first_attempt = ledger.create_execution_attempt(task_id)
    second_attempt = ledger.create_execution_attempt(task_id)
    first = _write_session(tmp_path / "first.jsonl")
    second = _write_session(tmp_path / "second.jsonl", session_id=OTHER_SESSION_ID)

    main(_link(repo, task_id, first, attempt_id=first_attempt.attempt_id))
    payload = json.loads(capsys.readouterr().out)
    assert payload["attempt_id"] == first_attempt.attempt_id

    main(_link(repo, task_id, second, attempt_id=second_attempt.attempt_id))
    payload = json.loads(capsys.readouterr().out)
    assert payload["attempt_id"] == second_attempt.attempt_id


def test_link_reuses_an_existing_empty_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    existing = _ledger(repo).create_execution_attempt(task_id)
    session = _write_session(tmp_path / "session.jsonl")

    main(_link(repo, task_id, session))
    payload = json.loads(capsys.readouterr().out)

    assert payload["attempt_id"] == existing.attempt_id
    assert len(_ledger(repo).execution_attempts(task_id)) == 1


def test_link_requires_session_and_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)

    error = _expect_exit(["outcomes", "link", task_id, "--repo", str(repo)], 2, capsys)
    assert "requires TASK_ID, --session PATH, and --agent AGENT" in error


def test_link_rejects_unknown_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session = _write_session(tmp_path / "session.jsonl")

    error = _expect_exit(_link(repo, "missing-task", session), 2, capsys)
    assert "Unknown task id" in error


def test_link_rejects_unknown_execution_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")

    error = _expect_exit(_link(repo, task_id, session, attempt_id="missing-attempt"), 2, capsys)
    assert "Unknown execution attempt" in error


def test_link_rejects_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)

    error = _expect_exit(_link(repo, task_id, tmp_path / "missing.jsonl"), 6, capsys)
    assert "does not exist" in error


def test_link_rejects_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    directory = tmp_path / "session.jsonl"
    directory.mkdir()

    error = _expect_exit(_link(repo, task_id, directory), 6, capsys)
    assert "regular file" in error


def test_link_rejects_symlink(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    target = _write_session(tmp_path / "target.jsonl")
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    error = _expect_exit(_link(repo, task_id, link), 6, capsys)
    assert "symbolic link" in error


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("session.json", '{"type":"user","sessionId":"s"}\n'),
        ("bad.jsonl", "{not json}\n"),
        ("array.jsonl", "[]\n"),
        ("no-messages.jsonl", '{"type":"summary","sessionId":"s"}\n'),
        ("non-string-type.jsonl", '{"type":[],"sessionId":"s"}\n'),
        (
            "no-message-object.jsonl",
            '{"type":"user","sessionId":"s","message":"hello"}\n',
        ),
        ("user-only.jsonl", '{"type":"user","sessionId":"s","message":{"role":"user"}}\n'),
    ],
)
def test_link_rejects_invalid_session_formats(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = tmp_path / name
    session.write_text(content, encoding="utf-8")

    _expect_exit(_link(repo, task_id, session), 6, capsys)


def test_link_rejects_missing_session_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    lines = _session_lines()
    del lines[0]["sessionId"]
    session = _write_session(tmp_path / "session.jsonl", lines=lines)

    error = _expect_exit(_link(repo, task_id, session), 6, capsys)
    assert "missing sessionId" in error


def test_link_rejects_mismatched_session_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    lines = _session_lines()
    lines[1]["sessionId"] = OTHER_SESSION_ID
    session = _write_session(tmp_path / "session.jsonl", lines=lines)

    error = _expect_exit(_link(repo, task_id, session), 6, capsys)
    assert "mismatched sessionId" in error


def test_link_rejects_invalid_session_id_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl", session_id="id with spaces")

    error = _expect_exit(_link(repo, task_id, session), 6, capsys)
    assert "bounded token" in error


def test_link_rejects_unsupported_agent(tmp_path: Path) -> None:
    session = _write_session(tmp_path / "session.jsonl")
    with pytest.raises(Exception, match="Unsupported agent session format"):
        validate_agent_session(session, "codex")


def test_link_rejects_repository_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")
    config = _shared_ledger_config(tmp_path, repo)

    error = _expect_exit(_link(other, task_id, session, config=config), 2, capsys)
    assert "does not match the task repository" in error


def test_link_rejects_conflicting_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")

    main(_link(repo, task_id, session))
    capsys.readouterr()
    second_attempt = _ledger(repo).create_execution_attempt(task_id)
    error = _expect_exit(
        _link(repo, task_id, session, attempt_id=second_attempt.attempt_id), 2, capsys
    )
    assert "different execution attempt" in error


def test_link_rejects_session_linked_to_another_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_task = _create_task(repo, "fix the first helper")
    second_task = _create_task(repo, "fix the second helper")
    session = _write_session(tmp_path / "session.jsonl")

    main(_link(repo, first_task, session))
    capsys.readouterr()
    error = _expect_exit(_link(repo, second_task, session), 2, capsys)
    assert "different task" in error


def test_link_rejects_changed_session_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")

    main(_link(repo, task_id, session))
    capsys.readouterr()
    lines = _session_lines()
    lines[1]["message"] = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Different evidence."}],
    }
    _write_session(session, lines=lines)

    error = _expect_exit(_link(repo, task_id, session), 2, capsys)
    assert "content changed" in error


def test_link_rejects_oversized_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")
    monkeypatch.setattr(session_linking, "MAX_SESSION_BYTES", 10)

    error = _expect_exit(_link(repo, task_id, session), 6, capsys)
    assert "safety limit" in error


def test_link_rejects_unreadable_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _write_session(tmp_path / "session.jsonl")

    def fail_open(*args: object, **kwargs: object) -> int:
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(session_linking.os, "open", fail_open)
    with pytest.raises(Exception, match="not readable"):
        validate_agent_session(session, "claude-code")


def test_link_enforces_size_limit_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _write_session(tmp_path / "session.jsonl")
    real_fstat = os.fstat

    def zero_size_stat(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        return os.stat_result(
            (
                info.st_mode,
                info.st_ino,
                info.st_dev,
                info.st_nlink,
                info.st_uid,
                info.st_gid,
                0,
                info.st_atime,
                info.st_mtime,
                info.st_ctime,
            )
        )

    monkeypatch.setattr(session_linking.os, "fstat", zero_size_stat)
    monkeypatch.setattr(session_linking, "MAX_SESSION_BYTES", 10)
    with pytest.raises(Exception, match="grew beyond"):
        validate_agent_session(session, "claude-code")


def test_link_rejects_invalid_path_characters() -> None:
    with pytest.raises(Exception, match="invalid"):
        validate_agent_session(Path("invalid\x00session.jsonl"), "claude-code")


def test_parent_directory_symlink_is_followed(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    session = _write_session(real_directory / "session.jsonl")
    linked_directory = tmp_path / "linked"
    try:
        linked_directory.symlink_to(real_directory, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    validated = validate_agent_session(linked_directory / session.name, "claude-code")
    assert validated.external_session_id == SESSION_ID


def test_symlink_check_without_o_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write_session(tmp_path / "target.jsonl")
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")
    monkeypatch.delattr(session_linking.os, "O_NOFOLLOW")

    with pytest.raises(Exception, match="symbolic link"):
        validate_agent_session(link, "claude-code")


def test_symlink_check_error_without_o_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _write_session(tmp_path / "session.jsonl")
    monkeypatch.delattr(session_linking.os, "O_NOFOLLOW")

    def fail_is_symlink(self: Path) -> bool:
        raise OSError("stat failed")

    monkeypatch.setattr(Path, "is_symlink", fail_is_symlink)
    with pytest.raises(Exception, match="does not exist"):
        validate_agent_session(session, "claude-code")


def test_fstat_symlink_mode_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _write_session(tmp_path / "session.jsonl")
    symlink_stat = os.stat_result((stat.S_IFLNK | 0o777, 1, 1, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(session_linking.os, "fstat", lambda descriptor: symlink_stat)

    with pytest.raises(Exception, match="symbolic link"):
        validate_agent_session(session, "claude-code")


def test_read_error_after_open_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _write_session(tmp_path / "session.jsonl")

    class FailingHandle:
        def __enter__(self) -> FailingHandle:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def readline(self, size: int) -> bytes:
            raise OSError("read failed")

    monkeypatch.setattr(session_linking, "_open_session_file", lambda path: FailingHandle())
    with pytest.raises(Exception, match="not readable"):
        validate_agent_session(session, "claude-code")


def test_linked_sessions_preserve_privacy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo, "fix sensitive helper")
    session = _write_session(tmp_path / "private-session.jsonl")
    main(_link(repo, task_id, session))
    capsys.readouterr()

    database = repo / ".tgv" / "outcomes.sqlite3"
    with sqlite3.connect(database) as connection:
        for table in ("execution_attempts", "agent_sessions", "ter_measurements"):
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
            assert not any(
                word in column
                for word in ("path", "jsonl", "prompt", "transcript", "raw")
                for column in columns
            )
        session_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(agent_sessions)")
        ]
        assert session_columns == [
            "session_link_id",
            "attempt_id",
            "agent",
            "external_session_id",
            "content_hash",
            "linked_at",
        ]
        raw_request = connection.execute(
            "SELECT raw_request FROM outcomes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        session_values = connection.execute(
            "SELECT * FROM agent_sessions WHERE attempt_id IN "
            "(SELECT attempt_id FROM execution_attempts WHERE task_id = ?)",
            (task_id,),
        ).fetchall()
    assert raw_request is None
    assert all(str(session) not in " ".join(map(str, row)) for row in session_values)
    assert all("Fix the helper bug" not in " ".join(map(str, row)) for row in session_values)

    payload = json.loads(_ledger(repo).export_json(_ledger(repo).query(limit=10)))
    assert str(session) not in json.dumps(payload)
    assert "Fix the helper bug" not in json.dumps(payload)


def test_validate_records_positive_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)

    main(
        [
            "outcomes",
            "validate",
            task_id,
            "--tests-passed",
            "--static-checks-passed",
            "--repo",
            str(repo),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "task_id": task_id,
        "tests_passed": True,
        "static_checks_passed": True,
    }
    record = _ledger(repo).query()[0]
    assert record.tests_passed is True
    assert record.static_checks_passed is True


def test_validate_records_negative_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)

    main(
        [
            "outcomes",
            "validate",
            task_id,
            "--tests-failed",
            "--static-checks-failed",
            "--repo",
            str(repo),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["tests_passed"] is False
    assert payload["static_checks_passed"] is False
    record = _ledger(repo).query()[0]
    assert record.tests_passed is False
    assert record.static_checks_passed is False


def test_validate_accepts_partial_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)

    main(["outcomes", "validate", task_id, "--tests-passed", "--repo", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["tests_passed"] is True
    assert payload["static_checks_passed"] is None


def test_validate_requires_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)

    error = _expect_exit(["outcomes", "validate", task_id, "--repo", str(repo)], 2, capsys)
    assert "requires --tests-passed" in error


def test_validate_requires_task_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    error = _expect_exit(["outcomes", "validate", "--tests-passed", "--repo", str(repo)], 2, capsys)
    assert "requires TASK_ID" in error


def test_validate_rejects_contradictory_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)

    _expect_exit(
        [
            "outcomes",
            "validate",
            task_id,
            "--tests-passed",
            "--tests-failed",
            "--repo",
            str(repo),
        ],
        2,
        capsys,
    )


def test_validate_rejects_unknown_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    error = _expect_exit(
        ["outcomes", "validate", "missing-task", "--tests-passed", "--repo", str(repo)],
        2,
        capsys,
    )
    assert "Unknown task id" in error


def test_validate_rejects_repository_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    task_id = _create_task(repo)
    config = _shared_ledger_config(tmp_path, repo)

    error = _expect_exit(
        [
            "outcomes",
            "validate",
            task_id,
            "--tests-passed",
            "--repo",
            str(other),
            "--config",
            str(config),
        ],
        2,
        capsys,
    )
    assert "does not match the current repository" in error


def test_completion_alone_is_not_verified_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    ledger = _ledger(repo)

    ledger.record_execution(task_id, status="completed")
    report = run_outcome_benchmark(ledger.query())
    assert report["overall"]["verified_samples"] == 0

    main(["outcomes", "validate", task_id, "--tests-passed", "--repo", str(repo)])
    capsys.readouterr()
    report = run_outcome_benchmark(_ledger(repo).query())
    assert report["overall"]["verified_samples"] == 1
    assert report["overall"]["verified_success_rate"] == 1.0


def test_existing_outcome_commands_are_compatible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    session = _write_session(tmp_path / "session.jsonl")

    main(_link(repo, task_id, session))
    capsys.readouterr()

    main(["outcomes", "list", "--repo", str(repo), "--format", "json"])
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["task_id"] == task_id
    assert "session_link_id" not in listed[0]

    main(["outcomes", "export", "--repo", str(repo), "--format", "csv"])
    exported = capsys.readouterr().out
    assert exported.startswith("task_id,")

    main(["outcomes", "complete", task_id, "--accepted", "--repo", str(repo)])
    completed = json.loads(capsys.readouterr().out)
    assert completed == {"task_id": task_id, "completed": True, "accepted": True}


def test_atomic_link_rolls_back_attempt_when_session_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    ledger = _ledger(repo)

    def fail_insert(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated session insert failure")

    monkeypatch.setattr(OutcomeLedger, "_insert_agent_session", staticmethod(fail_insert))
    with pytest.raises(RuntimeError, match="simulated session insert failure"):
        ledger.link_agent_session_for_task(
            task_id,
            agent="claude-code",
            external_session_id=SESSION_ID,
            content_hash="a" * 64,
        )

    assert _ledger(repo).execution_attempts(task_id) == []
    with sqlite3.connect(repo / ".tgv" / "outcomes.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0] == 0


def test_atomic_link_rejects_attempt_from_another_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_task = _create_task(repo, "fix the first helper")
    second_task = _create_task(repo, "fix the second helper")
    ledger = _ledger(repo)
    attempt = ledger.create_execution_attempt(first_task)

    with pytest.raises(CorrelationMismatchError, match="different task"):
        ledger.link_agent_session_for_task(
            second_task,
            agent="claude-code",
            external_session_id=SESSION_ID,
            content_hash="a" * 64,
            attempt_id=attempt.attempt_id,
        )
    assert len(_ledger(repo).agent_sessions(attempt.attempt_id)) == 0


def test_atomic_link_rejects_cross_repository_before_creating_attempt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    ledger = _ledger(repo)

    with pytest.raises(CrossRepositoryError, match="does not match"):
        ledger.link_agent_session_for_task(
            task_id,
            agent="claude-code",
            external_session_id=SESSION_ID,
            content_hash="a" * 64,
            expected_repository_id="other-repository",
        )
    assert _ledger(repo).execution_attempts(task_id) == []


def test_agent_session_lookup_is_namespaced_and_strips_input(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    ledger = _ledger(repo)
    assert ledger.agent_session("claude-code", SESSION_ID) is None

    _, link, _ = ledger.link_agent_session_for_task(
        task_id,
        agent="claude-code",
        external_session_id=SESSION_ID,
        content_hash="a" * 64,
    )
    found = ledger.agent_session(" claude-code ", f" {SESSION_ID} ")
    assert found == link


def test_concurrent_identical_linking_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    barrier = Barrier(2)

    def worker() -> tuple[str, str, bool]:
        ledger = _ledger(repo)
        barrier.wait()
        attempt, link, created = ledger.link_agent_session_for_task(
            task_id,
            agent="claude-code",
            external_session_id=SESSION_ID,
            content_hash="a" * 64,
        )
        return attempt.attempt_id, link.session_link_id, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(worker), pool.submit(worker)]]

    assert len({result[0] for result in results}) == 1
    assert len({result[1] for result in results}) == 1
    assert sorted(result[2] for result in results) == [False, True]
    ledger = _ledger(repo)
    attempts = ledger.execution_attempts(task_id)
    assert len(attempts) == 1
    assert len(ledger.agent_sessions(attempts[0].attempt_id)) == 1


def test_concurrent_distinct_linking_allocates_distinct_attempts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = _create_task(repo)
    barrier = Barrier(2)

    def worker(session_id: str, content_hash: str) -> tuple[str, str]:
        ledger = _ledger(repo)
        barrier.wait()
        attempt, link, created = ledger.link_agent_session_for_task(
            task_id,
            agent="claude-code",
            external_session_id=session_id,
            content_hash=content_hash,
        )
        assert created is True
        return attempt.attempt_id, link.external_session_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, SESSION_ID, "a" * 64),
            pool.submit(worker, OTHER_SESSION_ID, "b" * 64),
        ]
        results = [future.result() for future in futures]

    ledger = _ledger(repo)
    attempts = ledger.execution_attempts(task_id)
    assert len(attempts) == 2
    assert {attempt.attempt_id for attempt in attempts} == {result[0] for result in results}
    for attempt_id, external_session_id in results:
        links = ledger.agent_sessions(attempt_id)
        assert [link.external_session_id for link in links] == [external_session_id]
