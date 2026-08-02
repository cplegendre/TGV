import pytest

from tgv.config import default_config
from tgv.models import Route, RouteDecision, RoutingConfig
from tgv.router import Router


class FakeProvider:
    def route(self, request: str) -> RouteDecision:
        return RouteDecision(
            route=Route.LOCAL_FAST,
            confidence=0.99,
            rationale="Fake model",
        )


def test_security_task_forces_frontier() -> None:
    router = Router(default_config().routing, FakeProvider())
    decision = router.route("Redesign authentication and rotate production credentials")
    assert decision.route is Route.FRONTIER_DEEP
    assert decision.risk_signals


def test_basic_question_routes_fast_without_provider() -> None:
    router = Router(default_config().routing)
    decision = router.route("Where is the parser configured?")
    assert decision.route is Route.LOCAL_FAST


def test_bounded_fix_routes_code() -> None:
    router = Router(default_config().routing)
    decision = router.route("Fix typo in parser.py and add a test")
    assert decision.route is Route.LOCAL_CODE


@pytest.mark.parametrize(
    ("task", "category"),
    [
        ("Review this security vulnerability", "high-risk:security-sensitive"),
        ("Redesign the service architecture", "high-risk:architecture"),
        ("Fix a race condition", "high-risk:concurrency"),
        ("Drop database in production", "high-risk:destructive"),
    ],
)
def test_each_high_risk_category(task: str, category: str) -> None:
    decision = Router(default_config().routing).deterministic(task)
    assert decision.route is Route.FRONTIER_DEEP
    assert decision.matched_category == category
    assert decision.confidence == 0.92


def test_ambiguous_scope_fallback_is_explained() -> None:
    decision = Router(default_config().routing).deterministic("Improve things")
    assert decision.route is Route.FRONTIER_DEEP
    assert decision.matched_category == "ambiguous-scope"
    assert decision.confidence == 0.55


def test_simple_authentication_question_stays_local_fast() -> None:
    decision = Router(RoutingConfig(use_local_model=False)).deterministic(
        "Where is authentication configured?"
    )
    assert decision.route == Route.LOCAL_FAST


def test_strong_authentication_change_stays_frontier() -> None:
    decision = Router(RoutingConfig(use_local_model=False)).deterministic(
        "Redesign authentication and rotate production credentials"
    )
    assert decision.route == Route.FRONTIER_DEEP
    assert "security-sensitive" in decision.risk_signals


class FailingProvider:
    def route(self, request: str) -> RouteDecision:
        from tgv.exceptions import ModelProviderError

        raise ModelProviderError("provider unavailable")


@pytest.mark.parametrize(
    ("model_route", "confidence", "expected_route"),
    [
        (Route.LOCAL_FAST, 0.10, Route.LOCAL_FAST),
        (Route.LOCAL_CODE, 0.10, Route.LOCAL_FAST),
        (Route.LOCAL_FAST, 0.99, Route.LOCAL_FAST),
        (Route.LOCAL_CODE, 0.99, Route.LOCAL_CODE),
    ],
)
def test_model_threshold_and_acceptance_paths(
    model_route: Route, confidence: float, expected_route: Route
) -> None:
    class Provider:
        def route(self, request: str) -> RouteDecision:
            return RouteDecision(
                route=model_route,
                confidence=confidence,
                rationale="model decision",
            )

    decision = Router(RoutingConfig(use_local_model=True), Provider()).route("Where is the parser?")
    assert decision.route is expected_route
    if confidence >= 0.99:
        assert decision.decision_source == "model"


def test_provider_failure_returns_deterministic_baseline() -> None:
    router = Router(RoutingConfig(use_local_model=True), FailingProvider())
    decision = router.route("Improve things")
    assert decision.route is Route.FRONTIER_DEEP
    assert decision.matched_category == "ambiguous-scope"


def test_clear_large_task_routes_frontier_standard() -> None:
    request = (
        "Implement a feature across all modules with exact acceptance criteria, preserve the API, "
        "add integration tests, update documentation, and keep backward compatibility. " * 4
    )
    decision = Router(default_config().routing).deterministic(request)
    assert decision.route is Route.FRONTIER_STANDARD
    assert decision.matched_category == "frontier-standard"
