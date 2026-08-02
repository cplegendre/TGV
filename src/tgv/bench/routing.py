from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from tgv.config import default_config
from tgv.router import Router


def default_cases_path() -> Path:
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "routing_cases.jsonl"


def run_benchmark(path: Path) -> dict[str, Any]:
    router = Router(default_config().routing)
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    exact = acceptable = 0
    confusion: Counter[str] = Counter()
    categories: dict[str, list[bool]] = defaultdict(list)
    low_correct: list[str] = []
    high_wrong: list[str] = []
    high_risk_violations: list[str] = []
    for row in rows:
        decision = router.deterministic(row["request"])
        expected = row["expected_route"]
        allowed = {expected, *row.get("acceptable_alternatives", [])}
        is_exact = decision.route.value == expected
        is_acceptable = decision.route.value in allowed
        exact += is_exact
        acceptable += is_acceptable
        confusion[f"{expected}->{decision.route.value}"] += 1
        categories[row["category"]].append(is_acceptable)
        if is_acceptable and decision.confidence < row.get("minimum_confidence", 0.0):
            low_correct.append(row["request"])
        if not is_acceptable and decision.confidence >= 0.8:
            high_wrong.append(row["request"])
        if row.get("expected_risk_signals") and not decision.route.value.startswith("frontier_"):
            high_risk_violations.append(row["request"])
    total = len(rows)
    return {
        "total_cases": total,
        "accuracy": exact / total if total else 0.0,
        "acceptable_route_accuracy": acceptable / total if total else 0.0,
        "confusion_matrix": dict(sorted(confusion.items())),
        "low_confidence_correct": low_correct,
        "high_confidence_wrong": high_wrong,
        "per_category_accuracy": {
            key: sum(values) / len(values) for key, values in sorted(categories.items())
        },
        "high_risk_violations": high_risk_violations,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate deterministic TGV routing quality")
    parser.add_argument("--cases", type=Path, default=default_cases_path())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_benchmark(args.cases)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"cases: {report['total_cases']}")
    print(f"accuracy: {report['accuracy']:.2%}")
    print(f"acceptable-route accuracy: {report['acceptable_route_accuracy']:.2%}")
    print(f"high-risk violations: {len(report['high_risk_violations'])}")
    for category, score in report["per_category_accuracy"].items():
        print(f"  {category}: {score:.2%}")


if __name__ == "__main__":
    main()
