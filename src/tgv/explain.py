from __future__ import annotations

from dataclasses import dataclass

from tgv.models import Route, RouteDecision
from tgv.router import Router

_SIGNAL_LABELS = {
    "simple_extractive": "a simple/extractive question",
    "bounded_code": "a bounded code change",
    "ambiguous_scope": "ambiguous or broad scope",
    "high_risk": "a high-risk safety/security signal",
    "repository_aware": "repository-specific context",
    "explicit_constraints": "explicit constraints",
    "uncertainty": "request uncertainty",
    "frontier_scale": "large frontier-scale implementation scope",
}


@dataclass(frozen=True)
class Counterfactual:
    target_route: Route
    deterministic_score_gap: float
    changes: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "target_route": self.target_route.value,
            "deterministic_score_gap": self.deterministic_score_gap,
            "changes": list(self.changes),
            "note": self.note,
        }


def _change_description(signal: str, current: float) -> str:
    label = _SIGNAL_LABELS.get(signal, signal.replace("_", " "))
    return f"reduce/remove {label}" if current > 0 else f"add stronger evidence of {label}"


def _why_selected(router: Router, request: str, decision: RouteDecision) -> list[str]:
    features = router.extractor.extract(request)
    selected_weights = router.WEIGHTS[decision.route]
    contributions = sorted(
        (
            (signal, round(value * selected_weights.get(signal, 0.0), 3))
            for signal, value in features.signals.items()
            if value > 0 and selected_weights.get(signal, 0.0) > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )
    reasons = [
        f"{_SIGNAL_LABELS.get(signal, signal)} contributed +{score:g} to {decision.route.value}"
        for signal, score in contributions[:4]
    ]
    if decision.risk_signals:
        reasons.insert(0, f"safety/risk signals: {', '.join(decision.risk_signals)}")
    if decision.decision_source == "scoreboard" and decision.scoreboard_recommendation:
        recommendation = decision.scoreboard_recommendation
        reasons.append(
            "historical outcomes favored "
            f"{recommendation.recommended_model or 'the selected model'} "
            f"from {recommendation.sample_size} comparable samples"
        )
    elif decision.decision_source == "safety-override":
        reasons.append("the safety policy overrode adaptive or deterministic routing")
    if not reasons:
        reasons.append("the selected route had the strongest weighted request signal score")
    return reasons


def _counterfactual(
    router: Router,
    request: str,
    decision: RouteDecision,
    target: Route,
) -> Counterfactual:
    features = router.extractor.extract(request)
    scores = decision.score_breakdown or router.deterministic(request).score_breakdown
    selected_score = float(scores.get(decision.route.value, 0.0))
    target_score = float(scores.get(target.value, 0.0))
    gap = round(selected_score - target_score, 3)

    if decision.risk_signals and target in {Route.LOCAL_FAST, Route.LOCAL_CODE}:
        return Counterfactual(
            target_route=target,
            deterministic_score_gap=gap,
            changes=("remove or resolve the high-risk safety signal",),
            note="Local routing remains unavailable while the safety override applies.",
        )

    if decision.decision_source == "scoreboard" and target_score > selected_score:
        return Counterfactual(
            target_route=target,
            deterministic_score_gap=gap,
            changes=(
                "weaken the historical utility advantage for the currently selected model/tier",
                "collect enough successful outcome evidence for the target tier",
            ),
            note=(
                "Deterministic request signals already favor this route; historical outcome "
                "evidence is what keeps the current selection in place."
            ),
        )

    selected_weights = router.WEIGHTS[decision.route]
    target_weights = router.WEIGHTS[target]
    candidates: list[tuple[float, str]] = []
    for signal, current in features.signals.items():
        weight_delta = target_weights.get(signal, 0.0) - selected_weights.get(signal, 0.0)
        proposed_change = -current if current > 0 else 1.0
        improvement = weight_delta * proposed_change
        if improvement > 0.001:
            candidates.append((improvement, _change_description(signal, current)))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    needed = max(0.001, gap + 0.001)
    gained = 0.0
    changes: list[str] = []
    for improvement, description in candidates:
        changes.append(description)
        gained += improvement
        if gained >= needed:
            break

    if gap < 0:
        note = (
            "This route already has a higher deterministic score; model/history/safety policy "
            "is responsible for the effective selection."
        )
    elif gained >= needed:
        note = (
            "Estimated from the deterministic routing weights; this is explanatory, "
            "not a guarantee."
        )
    else:
        note = (
            "No small request-signal change is sufficient under the current deterministic weights; "
            "a broader scope or policy/history change would be needed."
        )
    if not changes:
        changes = ["change the request scope or accumulated routing evidence materially"]
    return Counterfactual(target, gap, tuple(changes), note)


def explain_decision(router: Router, request: str, decision: RouteDecision) -> dict[str, object]:
    """Build a stable, privacy-local explanation including route counterfactuals."""
    counterfactuals = [
        _counterfactual(router, request, decision, route)
        for route in Route
        if route != decision.route
    ]
    # Enum aliases can still surface the same value; keep one target per value.
    unique: dict[str, Counterfactual] = {}
    for item in counterfactuals:
        unique[item.target_route.value] = item
    return {
        "decision": decision.model_dump(mode="json"),
        "why_selected": _why_selected(router, request, decision),
        "what_would_change_the_decision": [
            item.to_dict()
            for item in sorted(unique.values(), key=lambda item: item.target_route.value)
        ],
        "disclaimer": (
            "Counterfactuals explain the current deterministic weights and policy/history inputs; "
            "they do not predict model quality."
        ),
    }


def render_explanation(report: dict[str, object]) -> str:
    decision = report["decision"]
    assert isinstance(decision, dict)
    lines = [
        f"Selected: {decision['route']}",
        f"Model: {decision.get('selected_model') or 'n/a'}",
        f"Capability: {decision.get('selected_capability_tier') or 'n/a'}",
        f"Source: {decision.get('decision_source', 'deterministic')}",
        f"Confidence: {float(decision.get('confidence', 0.0)):.0%}",
        "",
        "Why this route:",
    ]
    why = report["why_selected"]
    assert isinstance(why, list)
    lines.extend(f"  • {reason}" for reason in why)
    lines.extend(["", "What would make another route win:"])
    alternatives = report["what_would_change_the_decision"]
    assert isinstance(alternatives, list)
    for alternative in alternatives:
        assert isinstance(alternative, dict)
        lines.append(f"  {alternative['target_route']}:")
        changes = alternative["changes"]
        assert isinstance(changes, list)
        lines.extend(f"    - {change}" for change in changes)
        lines.append(f"    note: {alternative['note']}")
    return "\n".join(lines)
