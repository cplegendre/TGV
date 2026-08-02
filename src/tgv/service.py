from __future__ import annotations

from pathlib import Path

from tgv.compiler import ContextCompiler
from tgv.config import load_config
from tgv.intelligence import CodeKnowledgeBase, TaskProfiler
from tgv.models import (
    CapabilityTier,
    ContextBundle,
    HandoffMode,
    RetrievalItem,
    Route,
    RouteDecision,
    ScoreboardRecommendation,
    TaskProfile,
    TGVConfig,
)
from tgv.outcomes import OutcomeLedger
from tgv.providers import CommandHandoffProvider, create_local_provider
from tgv.repository import Repository
from tgv.router import Router
from tgv.scoreboard import RoutingScoreboard
from tgv.telemetry import SessionRecorder


class TGVService:
    """Orchestrate profiling, retrieval, safe routing, context compilation, and outcomes."""

    def __init__(self, config: TGVConfig, repository: Repository) -> None:
        self.config = config
        self.repository = repository
        fast = config.models.get("fast")
        provider = create_local_provider(fast)
        self.provider = provider
        self.router = Router(config.routing, provider)
        self.compiler = ContextCompiler(repository)
        self.profiler = TaskProfiler(config.intelligence)
        self.knowledge_base = CodeKnowledgeBase(repository, config.intelligence)
        outcome_path = config.intelligence.outcome_database_path
        if not outcome_path.is_absolute():
            outcome_path = repository.root / outcome_path
        self.outcomes = OutcomeLedger(
            outcome_path,
            retain_raw_prompts=config.intelligence.retain_raw_prompts,
        )
        self.scoreboard = RoutingScoreboard(self.outcomes, config.intelligence)
        log_path = config.telemetry.log_path
        if not log_path.is_absolute():
            log_path = repository.root / log_path
        self.recorder = SessionRecorder(log_path) if config.telemetry.enabled else None

    @classmethod
    def create(cls, repo_root: Path, config_path: Path | None = None) -> TGVService:
        config = load_config(config_path)
        repository = Repository(repo_root, config.repository, config.privacy.deny_patterns)
        return cls(config, repository)

    def _record(self, request: str, decision: RouteDecision) -> None:
        if self.recorder is not None:
            self.recorder.record(request, decision)

    def profile(self, request: str, *, retrieve: bool = True) -> TaskProfile:
        initial = self.profiler.profile(request)
        if not retrieve:
            return initial
        evidence = self.repository.search(
            request, limit=self.config.intelligence.maximum_retrieved_files
        )
        return self.profiler.profile(request, evidence, stage="revised")

    def retrieve(self, request: str) -> list[RetrievalItem]:
        return self.knowledge_base.retrieve(request)

    def route(self, request: str) -> RouteDecision:
        self.profiler.profile(request)
        evidence = self.repository.search(
            request, limit=self.config.intelligence.maximum_retrieved_files
        )
        revised = self.profiler.profile(request, evidence, stage="revised")
        baseline = self.router.route(request)
        recommendation = self.scoreboard.recommend(revised)
        decision = self._apply_recommendation(baseline, revised, recommendation)
        self._record(request, decision)
        return decision

    @staticmethod
    def _apply_recommendation(
        baseline: RouteDecision,
        profile: TaskProfile,
        recommendation: ScoreboardRecommendation,
    ) -> RouteDecision:
        scoreboard = recommendation
        if baseline.risk_signals or (
            profile.security_risk_score >= 70 and baseline.route != Route.LOCAL_FAST
        ):
            route = Route.FRONTIER_DEEP
            source = "safety-override"
            rationale = f"{baseline.rationale} Safety constraints override adaptive routing."
        elif scoreboard.fallback_used:
            route = baseline.route
            source = baseline.decision_source
            rationale = (
                f"{baseline.rationale} Historical evidence was insufficient; "
                "deterministic fallback retained."
            )
        else:
            route = TGVService._route_for_tier(scoreboard.recommended_capability_tier)
            source = "scoreboard"
            rationale = f"{baseline.rationale} {scoreboard.explanation}"
        return baseline.model_copy(
            update={
                "route": route,
                "decision_source": source,
                "rationale": rationale,
                "task_profile": profile,
                "scoreboard_recommendation": scoreboard,
            }
        )

    @staticmethod
    def _route_for_tier(tier: CapabilityTier | None) -> Route:
        if tier == CapabilityTier.BASIC:
            return Route.LOCAL_FAST
        if tier in {CapabilityTier.STANDARD, CapabilityTier.ADVANCED}:
            return Route.LOCAL_CODE
        return Route.FRONTIER_DEEP

    def create_outcome(self, request: str) -> str:
        profile = self.profiler.profile(request)
        return self.outcomes.create_pending(request, self.repository.root, profile)

    def prepare(self, request: str) -> ContextBundle:
        decision = self.route(request)
        bundle = self.compiler.compile(request, decision)
        profile = bundle.decision.task_profile or self.profiler.profile(request)
        task_id = self.outcomes.create_pending(request, self.repository.root, profile)
        tier = profile.recommended_capability_tier
        role_by_route = {
            Route.LOCAL_FAST: "fast",
            Route.LOCAL_CODE: "code",
            Route.FRONTIER_STANDARD: "frontier_standard",
            Route.FRONTIER_DEEP: "frontier_deep",
        }
        role = role_by_route[bundle.decision.route]
        model = self.config.models.get(role)
        self.outcomes.record_routing(
            task_id,
            revised_profile=profile,
            confidence=bundle.decision.confidence,
            model=model.model if model else role,
            capability_tier=tier,
            reasons=[bundle.decision.rationale],
            files=[e.path for e in bundle.evidence],
            symbols=[],
            context_token_estimate=sum(len(e.excerpt) for e in bundle.evidence) // 4,
        )
        bundle.assumptions.append(f"outcome_task_id={task_id}")
        return bundle

    def handoff(
        self,
        bundle: ContextBundle,
        output: Path,
        *,
        dry_run: bool = False,
        mode: HandoffMode | None = None,
    ) -> list[str] | None:
        selected = mode or self.config.handoff.mode
        self.compiler.write(bundle, output)
        if selected in {HandoffMode.HANDOFF_ONLY, HandoffMode.DISABLED}:
            return None
        md, js = self.compiler.frontier_handoff_paths(output)
        provider = CommandHandoffProvider(
            self.config.handoff.command,
            self.config.handoff.timeout_seconds,
            provider_name=self.config.handoff.provider_name,
            result_path=self.config.handoff.result_path,
        )
        metadata = provider.invoke_with_metadata(str(md), str(js), dry_run=dry_run)
        bundle.provider = metadata
        self.compiler.write(bundle, output)
        return metadata.command

    def close(self) -> None:
        """Release owned provider resources."""
        if self.provider is not None:
            self.provider.close()
