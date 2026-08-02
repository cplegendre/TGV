from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tgv.config import default_config
from tgv.intelligence import TaskProfiler
from tgv.models import TaskCategory
from tgv.router import Router


def default_cases_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "intelligence_cases.jsonl"


def _band(score: float) -> str:
    if score < 35:
        return "low"
    if score < 65:
        return "medium"
    return "high"


def run_benchmark(path: Path) -> dict[str, Any]:
    config = default_config()
    profiler = TaskProfiler(config.intelligence)
    router = Router(config.routing)
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    category_correct = 0
    band_correct = 0
    route_correct = 0
    false_low_risk = 0
    details: list[dict[str, Any]] = []
    for row in rows:
        profile = profiler.profile(row["request"])
        decision = router.deterministic(row["request"])
        category_ok = profile.task_category == TaskCategory(row["expected_task_category"])
        band_ok = _band(profile.complexity_score) == row["expected_complexity_band"]
        route_ok = decision.route.value in row["acceptable_routes"]
        risk_expected = bool(row.get("expected_risk_factors"))
        risk_missed = (
            risk_expected and profile.security_risk_score < 60 and not decision.risk_signals
        )
        category_correct += int(category_ok)
        band_correct += int(band_ok)
        route_correct += int(route_ok)
        false_low_risk += int(risk_missed)
        details.append(
            {
                "case_id": row["case_id"],
                "category_correct": category_ok,
                "complexity_band_correct": band_ok,
                "route_acceptable": route_ok,
                "false_low_risk": risk_missed,
            }
        )
    total = len(rows)
    report = {
        "total_cases": total,
        "task_category_accuracy": category_correct / total if total else 0.0,
        "complexity_band_accuracy": band_correct / total if total else 0.0,
        "acceptable_route_accuracy": route_correct / total if total else 0.0,
        "false_low_risk_rate": false_low_risk / total if total else 0.0,
        "details": details,
    }
    report["summary"] = (
        f"cases={total} category={report['task_category_accuracy']:.1%} "
        f"complexity={report['complexity_band_accuracy']:.1%} "
        f"routes={report['acceptable_route_accuracy']:.1%} "
        f"false-low-risk={report['false_low_risk_rate']:.1%}"
    )
    return report
