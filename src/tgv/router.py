from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType

from tgv.exceptions import ModelProviderError
from tgv.models import Route, RouteDecision, RoutingConfig
from tgv.providers import LocalModelProvider


@dataclass(frozen=True)
class RequestFeatures:
    signals: dict[str, float]
    matches: dict[str, list[str]]


class FeatureExtractor:
    REPO_TERMS = re.compile(
        r"\b(repo(?:sitory)?|module|package|file|path|test|cli|config|pyproject|src|docs?)\b", re.I
    )
    CONSTRAINTS = re.compile(
        r"\b(must|do not|don't|preserve|only|without|at least|exactly|keep|avoid)\b", re.I
    )
    UNCERTAINTY = re.compile(
        r"\b(maybe|possibly|unclear|unknown|unsure|might|could be|not sure|take a look|something (?:is )?(?:odd|strange))\b",
        re.I,
    )
    BROAD = re.compile(
        r"\b(entire|full|all (?:files|modules|services|issues|code)|production[- ]quality|comprehensive|across the codebase|end[- ]to[- ]end)\b",  # noqa: E501
        re.I,
    )

    def __init__(self, config: RoutingConfig) -> None:
        self.config = config

    def extract(self, request: str) -> RequestFeatures:
        matches: dict[str, list[str]] = {}
        simple = bool(
            re.search(self.config.patterns.simple, request, re.I) or request.strip().endswith("?")
        )
        code = bool(re.search(self.config.patterns.code, request, re.I))
        high = [
            name
            for name, pattern in self.config.patterns.high_risk.items()
            if re.search(pattern, request, re.I)
        ]
        repo = self.REPO_TERMS.findall(request)
        constraints = self.CONSTRAINTS.findall(request)
        uncertainty = self.UNCERTAINTY.findall(request)
        broad = self.BROAD.findall(request)
        if high:
            matches["high_risk"] = high
        if repo:
            matches["repository_aware"] = sorted({x.lower() for x in repo})
        if constraints:
            matches["explicit_constraints"] = sorted({x.lower() for x in constraints})
        if uncertainty:
            matches["uncertainty"] = sorted({x.lower() for x in uncertainty})
        if broad:
            matches["ambiguous_scope"] = sorted({x.lower() for x in broad})
        signals = {
            "simple_extractive": 1.0 if simple else 0.0,
            "bounded_code": 1.0
            if code and len(request.split()) <= 80 and not broad
            else (0.5 if code else 0.0),
            "ambiguous_scope": 1.0
            if broad or uncertainty or (not simple and not code and len(request.split()) < 4)
            else 0.0,
            "high_risk": 1.0 if high else 0.0,
            "repository_aware": min(1.0, len(repo) / 3),
            "explicit_constraints": min(1.0, len(constraints) / 3),
            "uncertainty": min(1.0, len(uncertainty) / 2),
            "frontier_scale": 1.0
            if code and (broad or len(request.split()) > 80) and not high and not uncertainty
            else 0.0,
        }
        return RequestFeatures(signals, matches)


class Router:
    WEIGHTS = MappingProxyType(
        {
            Route.LOCAL_FAST: {
                "simple_extractive": 3.2,
                "bounded_code": -1.2,
                "ambiguous_scope": -2.0,
                "high_risk": -8.0,
                "repository_aware": 0.2,
                "explicit_constraints": 0.1,
                "uncertainty": -1.0,
                "frontier_scale": -2.0,
            },
            Route.LOCAL_CODE: {
                "simple_extractive": -0.5,
                "bounded_code": 3.5,
                "ambiguous_scope": -1.8,
                "high_risk": -8.0,
                "repository_aware": 0.8,
                "explicit_constraints": 0.4,
                "uncertainty": -0.8,
                "frontier_scale": -0.8,
            },
            Route.FRONTIER_STANDARD: {
                "simple_extractive": -0.6,
                "bounded_code": 0.2,
                "ambiguous_scope": -0.4,
                "high_risk": -6.0,
                "repository_aware": 0.4,
                "explicit_constraints": 0.8,
                "uncertainty": -0.5,
                "frontier_scale": 4.2,
            },
            Route.FRONTIER_DEEP: {
                "simple_extractive": -0.4,
                "bounded_code": -0.2,
                "ambiguous_scope": 2.8,
                "high_risk": 10.0,
                "repository_aware": 0.2,
                "explicit_constraints": 0.4,
                "uncertainty": 1.8,
                "frontier_scale": 0.8,
            },
        }
    )

    def __init__(self, config: RoutingConfig, provider: LocalModelProvider | None = None) -> None:
        self.config, self.provider, self.extractor = config, provider, FeatureExtractor(config)

    def deterministic(self, request: str) -> RouteDecision:
        features = self.extractor.extract(request)
        scores = {
            route: sum(features.signals[k] * weight for k, weight in weights.items())
            for route, weights in self.WEIGHTS.items()
        }
        if features.signals["high_risk"] and self.config.force_frontier_for_high_risk:
            selected = Route.FRONTIER_DEEP
        else:
            selected = max(scores, key=lambda route: (scores[route], route.value))
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
        margin = ordered[0][1] - ordered[1][1]
        confidence = (
            0.92
            if features.signals["high_risk"]
            else (
                0.78
                if selected == Route.LOCAL_FAST
                else (
                    0.72
                    if selected == Route.LOCAL_CODE
                    else (0.64 if selected == Route.FRONTIER_STANDARD else 0.55)
                )
            )
        )
        matched = [name for name, value in features.signals.items() if value > 0]
        risk = features.matches.get("high_risk", [])
        if features.signals["ambiguous_scope"] and "ambiguous-scope" not in risk:
            risk = [*risk, "ambiguous-scope"]
        rationale = f"Selected {selected.value} from weighted request signals; top-score margin {margin:.2f}."  # noqa: E501
        category = (
            "high-risk:" + risk[0]
            if features.signals["high_risk"]
            else (
                "code"
                if selected == Route.LOCAL_CODE
                else (
                    "simple"
                    if selected == Route.LOCAL_FAST
                    else (
                        "frontier-standard"
                        if selected == Route.FRONTIER_STANDARD
                        else "ambiguous-scope"
                    )
                )
            )
        )
        return RouteDecision(
            route=selected,
            confidence=confidence,
            rationale=rationale,
            risk_signals=risk,
            search_terms=self._search_terms(request),
            matched_category=category,
            matched_pattern=None,
            matched_signals=matched,
            score_breakdown={r.value: round(v, 3) for r, v in scores.items()},
            alternative_routes={r.value: round(v, 3) for r, v in ordered if r != selected},
            suggested_validation=[
                "Run focused tests for affected behavior",
                "Run static analysis before handoff",
            ],
        )

    def route(self, request: str) -> RouteDecision:
        baseline = self.deterministic(request)
        if "high_risk" in baseline.matched_signals and self.config.force_frontier_for_high_risk:
            return baseline
        if not self.config.use_local_model or self.provider is None:
            return baseline
        try:
            model_decision = self.provider.route(request)
        except ModelProviderError:
            return baseline
        if (
            model_decision.route == Route.LOCAL_FAST
            and model_decision.confidence < self.config.local_answer_confidence
        ):
            return baseline
        if (
            model_decision.route == Route.LOCAL_CODE
            and model_decision.confidence < self.config.local_code_confidence
        ):
            return baseline
        return model_decision.model_copy(
            update={
                "decision_source": "model",
                "score_breakdown": model_decision.score_breakdown or baseline.score_breakdown,
                "matched_signals": model_decision.matched_signals or baseline.matched_signals,
                "alternative_routes": model_decision.alternative_routes
                or baseline.alternative_routes,
            }
        )

    @staticmethod
    def _search_terms(request: str) -> list[str]:
        ignored = {
            "the",
            "and",
            "for",
            "with",
            "this",
            "that",
            "from",
            "into",
            "where",
            "what",
            "please",
        }
        return list(
            dict.fromkeys(
                term.lower()
                for term in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", request)
                if term.lower() not in ignored
            )
        )[:12]
