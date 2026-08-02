from __future__ import annotations

import json
from pathlib import Path

from tgv.config import default_config
from tgv.efficiency import ScoredSession, aggregate_by_route
from tgv.router import Router

FIXTURES = Path(__file__).parent / "fixtures"


def test_route_efficiency_does_not_regress_below_baseline() -> None:
    router = Router(default_config().routing)
    sessions = []
    for line in (FIXTURES / "efficiency_sessions.jsonl").read_text().splitlines():
        item = json.loads(line)
        route = router.deterministic(item["request"]).route.value
        sessions.append(
            ScoredSession(
                route=route,
                aggregate_ter=item["aggregate_ter"],
                aligned_tokens=item["aligned_tokens"],
                waste_tokens=item["waste_tokens"],
            )
        )

    actual = aggregate_by_route(sessions)
    baseline = json.loads((FIXTURES / "efficiency_baseline.json").read_text())
    failures = []
    for route, minimum in baseline.items():
        observed = float(actual.get(route, {}).get("aggregate_ter", 0.0))
        if observed < minimum:
            failures.append(
                f"{route}: {observed:.4f} < {minimum:.4f} (delta {observed - minimum:.4f})"
            )
    assert not failures, (
        "Efficiency regression detected:\n"
        + "\n".join(failures)
        + "\nIf intentional, update tests/fixtures/efficiency_baseline.json "
        "with review justification."
    )
