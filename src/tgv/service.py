from __future__ import annotations

import time
from pathlib import Path

from tgv.compiler import ContextCompiler
from tgv.config import load_config
from tgv.exceptions import TGVError
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
from tgv.observability import ObservedProvider, RuntimeMetrics
from tgv.outcomes import OutcomeLedger
from tgv.providers import CommandHandoffProvider, create_local_provider
from tgv.repository import Repository
from tgv.router import Router
from tgv.scoreboard import RoutingScoreboard
from tgv.telemetry import SessionRecorder


class TGVService:
    """Orchestrate profiling, retrieval, safe routing, context compilation, and outcomes."""

    def __init__(
        self, config: TGVConfig, repository: Repository, *, read_only: bool = False
    ) -> None:
        self.config = config
        self.repository = repository
        self.read_only = read_only
        if read_only:
            self.repository.persist_index = False
        self.metrics = RuntimeMetrics()
        fast = config.models.get("fast")
        provider = create_local_provider(fast)
        self.provider = (
            ObservedProvider(provider, self.metrics, name=fast.provider, model=fast.model)
            if provider is not None and fast is not None
            else None
        )
        self.router = Router(config.routing, self.provider)
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
        self.recorder = (
            SessionRecorder(log_path) if config.telemetry.enabled and not read_only else None
        )

    @classmethod
    def create(
        cls,
        repo_root: Path,
        config_path: Path | None = None,
        *,
        read_only: bool = False,
    ) -> TGVService:
        config = load_config(config_path)
        repository = Repository(
            repo_root,
            config.repository,
            config.privacy.deny_patterns,
            persist_index=not read_only,
        )
        return cls(config, repository, read_only=read_only)

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
        started = time.monotonic()
        try:
            decision = self._route_impl(request)
        except Exception:
            self.metrics.record_route_error((time.monotonic() - started) * 1000)
            raise
        self.metrics.record_route(decision.route.value, (time.monotonic() - started) * 1000)
        return decision

    def _route_impl(self, request: str) -> RouteDecision:
        baseline = self.router.route(request)
        if not self.config.intelligence.enabled:
            decision = self._with_execution_plan(baseline)
            self._record(request, decision)
            return decision

        self.profiler.profile(request)
        evidence = self.repository.search(
            request, limit=self.config.intelligence.maximum_retrieved_files
        )
        revised = self.profiler.profile(request, evidence, stage="revised")
        if self.read_only:
            decision = self._with_execution_plan(
                baseline.model_copy(
                    update={
                        "task_profile": revised,
                        "rationale": (
                            f"{baseline.rationale} Read-only mode skipped adaptive outcome scoring."
                        ),
                    }
                )
            )
        else:
            recommendation = self.scoreboard.recommend(
                revised, repository_id=self.outcomes.repository_id(self.repository.root)
            )
            decision = self._with_execution_plan(
                self._apply_recommendation(baseline, revised, recommendation)
            )
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
                "selected_model": (
                    scoreboard.recommended_model if source == "scoreboard" else None
                ),
                "selected_capability_tier": (
                    scoreboard.recommended_capability_tier if source == "scoreboard" else None
                ),
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

    @staticmethod
    def _tier_for_route(route: Route) -> CapabilityTier:
        if route == Route.LOCAL_FAST:
            return CapabilityTier.BASIC
        if route == Route.LOCAL_CODE:
            return CapabilityTier.STANDARD
        if route == Route.FRONTIER_STANDARD:
            return CapabilityTier.ADVANCED
        return CapabilityTier.FRONTIER

    def _model_for_route(self, route: Route) -> str:
        role_by_route = {
            Route.LOCAL_FAST: "fast",
            Route.LOCAL_CODE: "code",
            Route.FRONTIER_STANDARD: "frontier_standard",
            Route.FRONTIER_DEEP: "frontier_deep",
        }
        role = role_by_route[route]
        model = self.config.models.get(role)
        return model.model if model else role

    def _with_execution_plan(self, decision: RouteDecision) -> RouteDecision:
        return decision.model_copy(
            update={
                "selected_model": decision.selected_model or self._model_for_route(decision.route),
                "selected_capability_tier": (
                    decision.selected_capability_tier or self._tier_for_route(decision.route)
                ),
            }
        )

    def explain(self, request: str) -> dict[str, object]:
        """Explain the effective route and estimate counterfactual route changes."""
        from tgv.explain import explain_decision

        decision = self.route(request)
        return explain_decision(self.router, request, decision)

    def create_outcome(self, request: str) -> str:
        if self.read_only or not self.config.intelligence.enabled:
            raise TGVError(
                "Outcome persistence is disabled in read-only/intelligence-disabled mode"
            )
        profile = self.profiler.profile(request)
        return self.outcomes.create_pending(request, self.repository.root, profile)

    def prepare(self, request: str) -> ContextBundle:
        decision = self.route(request)
        bundle = self.compiler.compile(request, decision)
        if self.read_only or not self.config.intelligence.enabled:
            return bundle

        profile = bundle.decision.task_profile or self.profiler.profile(request)
        task_id = self.outcomes.create_pending(request, self.repository.root, profile)
        tier = bundle.decision.selected_capability_tier or profile.recommended_capability_tier
        model = bundle.decision.selected_model or self._model_for_route(bundle.decision.route)
        self.outcomes.record_routing(
            task_id,
            revised_profile=profile,
            confidence=bundle.decision.confidence,
            model=model,
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
