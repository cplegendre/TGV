from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgv.efficiency import TERAnalysisError, _parse_ter_payload
from tgv.exceptions import ValidationFailure
from tgv.session_linking import supported_agents, validate_agent_session


def test_versioned_ter_3_contract(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "session_id": "s1",
        "aggregate_ter": 0.75,
        "aligned_tokens": 3,
        "waste_tokens": 1,
        "total_tokens": 4,
    }
    assert _parse_ter_payload(json.dumps(payload), tmp_path, "3.1.0").schema_version == 1
    payload["schema_version"] = 2
    with pytest.raises(TERAnalysisError, match="schema_version"):
        _parse_ter_payload(json.dumps(payload), tmp_path, "3.1.0")
    del payload["schema_version"]
    with pytest.raises(TERAnalysisError, match="Unsupported TER CLI"):
        _parse_ter_payload(json.dumps(payload), tmp_path, "3.1.0")


def test_codex_rollout_validation(tmp_path: Path) -> None:
    assert "codex" in supported_agents()
    path = tmp_path / "rollout-s1.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "s1", "cli_version": "0.164.0"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix a bug"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Done"}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    valid = validate_agent_session(path, "codex")
    assert valid.external_session_id == "s1"
    records[2]["payload"]["message"] = ""
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    with pytest.raises(ValidationFailure, match="meaningful"):
        validate_agent_session(path, "codex")


def test_codex_response_item_messages_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "rollout-response-items.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "s2", "session_id": "s2"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix a bug"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert validate_agent_session(path, "codex").external_session_id == "s2"


@pytest.mark.parametrize(
    ("name", "records", "message"),
    [
        (
            "session.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "s"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "u"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "a"}},
            ],
            "rollout-",
        ),
        (
            "rollout-no-meta.jsonl",
            [
                {"type": "event_msg", "payload": {"type": "user_message", "message": "u"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "a"}},
            ],
            "session_meta",
        ),
        (
            "rollout-conflict.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "one"}},
                {"type": "session_meta", "payload": {"id": "two"}},
            ],
            "Conflicting",
        ),
    ],
)
def test_codex_validation_rejects_ambiguous_sessions(
    tmp_path: Path, name: str, records: list[dict], message: str
) -> None:
    path = tmp_path / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    with pytest.raises(ValidationFailure, match=message):
        validate_agent_session(path, "codex")


def test_codex_validation_rejects_malformed_json_and_symlinks(tmp_path: Path) -> None:
    malformed = tmp_path / "rollout-malformed.jsonl"
    malformed.write_text("{not json}\n")
    with pytest.raises(ValidationFailure, match="Invalid Codex JSON"):
        validate_agent_session(malformed, "codex")

    target = tmp_path / "rollout-target.jsonl"
    target.write_text("")
    link = tmp_path / "rollout-link.jsonl"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValidationFailure, match="symbolic link"):
        validate_agent_session(link, "codex")


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([[]], "type and payload objects"),
        ([{"type": "session_meta", "payload": {}}], "missing session identity"),
        (
            [
                {"type": "session_meta", "payload": {"id": "s"}},
                {"type": "turn_context", "payload": {}},
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": []},
                },
            ],
            "meaningful user and assistant",
        ),
    ],
)
def test_codex_validation_rejects_incomplete_envelopes(
    tmp_path: Path, records: list[object], message: str
) -> None:
    path = tmp_path / "rollout-incomplete.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(ValidationFailure, match=message):
        validate_agent_session(path, "codex")
