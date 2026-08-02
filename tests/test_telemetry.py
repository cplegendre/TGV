from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgv.config import default_config
from tgv.models import Route, RouteDecision
from tgv.repository import Repository
from tgv.service import TGVService
from tgv.telemetry import SessionRecorder


def decision() -> RouteDecision:
    return RouteDecision(
        route=Route.LOCAL_FAST,
        confidence=0.91,
        rationale="Simple repository question",
        matched_category="simple",
        matched_pattern="where",
        risk_signals=[],
    )


def test_recorder_writes_ter_compatible_route_metadata(tmp_path: Path) -> None:
    path = SessionRecorder(tmp_path).record("Where is auth?", decision())
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert [entry["type"] for entry in entries] == ["user", "assistant"]
    assert entries[0]["route_decision"]["route"] == "local_fast"
    assert entries[1]["route_decision"]["matched_category"] == "simple"

    loader = pytest.importorskip("ter_calculator.loader")
    session = loader.load_session(path)
    route_blocks = [
        block
        for message in session.messages
        for block in message.content_blocks
        if block.block_type == "tgv_route_decision"
    ]
    assert len(route_blocks) == 1
    assert json.loads(route_blocks[0].text)["route"] == "local_fast"


def test_telemetry_is_disabled_by_default(sample_repo: Path) -> None:
    config = default_config()
    service = TGVService(config, Repository(sample_repo, config.repository, []))
    service.route("Where is auth?")
    assert not (sample_repo / ".tgv" / "sessions").exists()


def test_telemetry_never_logs_repository_contents(sample_repo: Path) -> None:
    secret_marker = "REPOSITORY_CONTENT_MUST_NOT_LEAK"
    (sample_repo / "private.py").write_text(secret_marker)
    config = default_config()
    config.telemetry.enabled = True
    service = TGVService(config, Repository(sample_repo, config.repository, []))
    service.prepare("Explain the authentication module")
    logs = list((sample_repo / ".tgv" / "sessions").glob("*.jsonl"))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert secret_marker not in text
    assert "Explain the authentication module" in text


def test_outcome_record_is_privacy_minimal() -> None:
    from tgv.models import OutcomeRecord

    outcome = OutcomeRecord(session_id="s1", succeeded=True, estimated_cost=0.1)
    assert "excerpt" not in outcome.model_dump()
    assert outcome.succeeded is True
