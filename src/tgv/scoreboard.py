from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean

from tgv.models import (
    CandidateScore,
    CapabilityTier,
    IntelligenceConfig,
    OutcomeTask,
    ScoreboardRecommendation,
    TaskProfile,
)
from tgv.outcomes import OutcomeLedger


class RoutingScoreboard:
    """Aggregate observed outcomes into statistically guarded routing candidates."""

    def __init__(self, ledger: OutcomeLedger, config: IntelligenceConfig) -> None:
        self.ledger = ledger
        self.config = config

    def recommend(
        self, profile: TaskProfile, *, repository_id: str | None = None
    ) -> ScoreboardRecommendation:
        records = self.ledger.query(
            category=profile.task_category, repository_id=repository_id, limit=10000
        )
        groups: dict[tuple[str, CapabilityTier], list[OutcomeTask]] = defaultdict(list)
        for record in records:
            if record.selected_model and record.selected_capability_tier:
                groups[(record.selected_model, record.selected_capability_tier)].append(record)
        candidates = [
            self._candidate(model, tier, items) for (model, tier), items in groups.items()
        ]
        eligible = [
            c for c in candidates if c.sample_size >= self.config.scoreboard_minimum_samples
        ]
        if not eligible:
            return ScoreboardRecommendation(
                alternative_candidates=sorted(
                    candidates, key=lambda item: (-item.utility_score, item.model)
                ),
                explanation=(
                    "Insufficient historical observations for this task category; "
                    "use the deterministic router."
                ),
                fallback_used=True,
                fallback_reason=(
                    f"scoreboard_minimum_samples={self.config.scoreboard_minimum_samples} "
                    "was not met for any candidate"
                ),
            )
        eligible.sort(key=lambda item: (-item.utility_score, -item.confidence, item.model))
        best = eligible[0]
        return ScoreboardRecommendation(
            recommended_model=best.model,
            recommended_capability_tier=best.capability_tier,
            expected_success_probability=best.success_probability,
            expected_cost=best.expected_cost,
            expected_latency=best.expected_latency,
            utility_score=best.utility_score,
            confidence=best.confidence,
            sample_size=best.sample_size,
            alternative_candidates=eligible[1:4],
            explanation=(
                f"Selected {best.model} from {best.sample_size} comparable outcomes using "
                "Bayesian-smoothed success and inspectable utility components."
            ),
        )

    def _candidate(
        self, model: str, tier: CapabilityTier, records: list[OutcomeTask]
    ) -> CandidateScore:
        sample_size = len(records)
        successes = sum(self._success(record) for record in records)
        success_probability = (successes + 1.0) / (sample_size + 2.0)
        tests = self._boolean_rate(records, "tests_passed")
        static = self._boolean_rate(records, "static_checks_passed")
        accepted = self._boolean_rate(records, "human_accepted")
        rework = self._boolean_rate(records, "rework_required")
        safety = self._boolean_rate(records, "safety_violation")
        retry_rate = mean(record.retry_count > 0 for record in records) if records else 0.0
        cost = mean(record.estimated_cost or 0.0 for record in records)
        latency = mean(record.latency_seconds or 0.0 for record in records)
        quality = mean([tests, static])
        weights = self.config.utility_weights
        components = {
            "success": success_probability * weights.get("success", 100.0),
            "quality": quality * weights.get("quality", 20.0),
            "acceptance": accepted * weights.get("acceptance", 20.0),
            "cost_penalty": -cost * weights.get("cost", 4.0),
            "latency_penalty": -latency * weights.get("latency", 0.05),
            "rework_penalty": -rework * weights.get("rework", 25.0),
            "retry_penalty": -retry_rate * weights.get("retry", 8.0),
            "safety_penalty": -safety * weights.get("safety", 1000.0),
        }
        confidence = min(
            1.0, math.sqrt(sample_size / self.config.scoreboard_minimum_samples) * 0.75
        )
        return CandidateScore(
            model=model,
            capability_tier=tier,
            sample_size=sample_size,
            success_probability=round(success_probability, 4),
            expected_cost=round(cost, 6),
            expected_latency=round(latency, 4),
            utility_score=round(sum(components.values()), 4),
            confidence=round(confidence, 4),
            components={key: round(value, 4) for key, value in components.items()},
        )

    @staticmethod
    def _success(record: OutcomeTask) -> int:
        if record.safety_violation:
            return 0
        if record.execution_status not in {"completed", "succeeded"}:
            return 0
        if record.tests_passed is False or record.static_checks_passed is False:
            return 0
        if record.human_accepted is False or record.rework_required is True:
            return 0
        observed_quality = (
            record.tests_passed is not None
            or record.static_checks_passed is not None
            or record.human_accepted is not None
            or record.rework_required is not None
        )
        return int(observed_quality)

    @staticmethod
    def _boolean_rate(records: list[OutcomeTask], field: str) -> float:
        values = [
            getattr(record, field) for record in records if getattr(record, field) is not None
        ]
        return mean(bool(value) for value in values) if values else 0.0
