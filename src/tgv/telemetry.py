from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from tgv.models import RouteDecision
from tgv.observability import atomic_text


class SessionRecorder:
    """Write privacy-minimal TGV interactions in TER-compatible Claude JSONL."""

    def __init__(self, log_directory: Path) -> None:
        self.log_directory = log_directory

    def record(
        self,
        request: str,
        decision: RouteDecision,
        *,
        metadata: dict[str, Any] | None = None,
        usage_tokens: int | None = None,
    ) -> Path:
        session_id = str(uuid4())
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        user_uuid = str(uuid4())
        assistant_uuid = str(uuid4())
        route_metadata = decision.model_dump(mode="json")
        route_metadata["route"] = decision.route.value
        record_metadata = dict(metadata or {})

        user_entry = {
            "type": "user",
            "uuid": user_uuid,
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": timestamp,
            "route_decision": route_metadata,
            "metadata": record_metadata,
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": request},
                    {
                        "type": "tgv_route_decision",
                        "text": json.dumps(route_metadata, sort_keys=True),
                    },
                ],
            },
        }
        response_text = (
            f"TGV routed this request to {decision.route.value} with confidence "
            f"{decision.confidence:.2f}. {decision.rationale}"
        )
        assistant_entry = {
            "type": "assistant",
            "uuid": assistant_uuid,
            "parentUuid": user_uuid,
            "sessionId": session_id,
            "requestId": str(uuid4()),
            "timestamp": timestamp,
            "route_decision": route_metadata,
            "metadata": record_metadata,
            "message": {
                "id": assistant_uuid,
                "role": "assistant",
                "model": "tgv-router",
                "content": [{"type": "text", "text": response_text}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 0 if usage_tokens is not None else max(1, len(request) // 4),
                    "output_tokens": (
                        usage_tokens
                        if usage_tokens is not None
                        else max(1, len(response_text) // 4)
                    ),
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }

        self.log_directory.mkdir(parents=True, exist_ok=True)
        path = self.log_directory / f"{session_id}.jsonl"
        atomic_text(
            path,
            "\n".join(
                json.dumps(entry, separators=(",", ":")) for entry in (user_entry, assistant_entry)
            )
            + "\n",
        )
        return path
