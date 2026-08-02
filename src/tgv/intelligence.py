from __future__ import annotations

import re
from collections.abc import Sequence

from tgv.models import (
    CapabilityTier,
    Evidence,
    IntelligenceConfig,
    RetrievalItem,
    TaskCategory,
    TaskProfile,
)
from tgv.repository import Repository

_CATEGORY_PATTERNS: tuple[tuple[TaskCategory, re.Pattern[str]], ...] = (
    (
        TaskCategory.SECURITY,
        re.compile(
            r"\b(auth(?:entication|orization)?|permission|credentials?|secret|security|vulnerab\w*|exploit|csrf|xss)\b",
            re.I,
        ),
    ),
    (
        TaskCategory.ARCHITECTURE,
        re.compile(r"\b(architect|microservice|redesign|migration|interface|distributed)\b", re.I),
    ),
    (
        TaskCategory.DEPENDENCY_CHANGE,
        re.compile(r"\b(dependenc|upgrade|downgrade|package|lockfile|pyproject)\b", re.I),
    ),
    (
        TaskCategory.PERFORMANCE,
        re.compile(r"\b(performance|latency|throughput|benchmark|optimi[sz]|memory)\b", re.I),
    ),
    (TaskCategory.TESTING, re.compile(r"\b(test|coverage|fixture|pytest|assertion)\b", re.I)),
    (
        TaskCategory.REFACTOR,
        re.compile(r"\b(refactor|restructure|cleanup|extract|rename across)\b", re.I),
    ),
    (
        TaskCategory.BUG_FIX,
        re.compile(r"\b(fix|bug|debug|regression|exception|traceback|broken|fails?)\b", re.I),
    ),
    (
        TaskCategory.FEATURE,
        re.compile(r"\b(add|implement|feature|support|introduce|create)\b", re.I),
    ),
    (
        TaskCategory.DOCUMENTATION,
        re.compile(r"\b(readme|documentation|docs?|changelog|comment)\b", re.I),
    ),
    (TaskCategory.FORMATTING, re.compile(r"\b(format|whitespace|style|lint-only|typo)\b", re.I)),
    (
        TaskCategory.REPOSITORY_INVESTIGATION,
        re.compile(r"\b(find|locate|inspect|where|which file|repository|codebase)\b", re.I),
    ),
)


class TaskProfiler:
    """Produce deterministic, inspectable 0-100 task estimates.

    Confidence is represented on a 0-1 scale. Complexity dimensions use 0-100.
    Repository evidence may revise the initial estimate but never suppresses explicit
    request-level security signals.
    """

    def __init__(self, config: IntelligenceConfig) -> None:
        self.config = config

    def profile(
        self,
        request: str,
        evidence: Sequence[Evidence] = (),
        *,
        stage: str = "initial",
    ) -> TaskProfile:
        category, category_confidence = self._category(request)
        words = request.split()
        paths = sorted({item.path for item in evidence})
        symbols = sorted({symbol for item in evidence for symbol in item.matched_symbols})
        broad = bool(
            re.search(
                r"\b(all|entire|across|production-grade|end-to-end|multiple)\b", request, re.I
            )
        )
        uncertain = len(
            re.findall(
                r"\b(maybe|possibly|unclear|unknown|might|not sure|investigate)\b", request, re.I
            )
        )
        security = category == TaskCategory.SECURITY or bool(
            re.search(
                r"\b(prod(?:uction)?|destructive|delete|rotate keys|credential)\b", request, re.I
            )
        )
        architecture = category == TaskCategory.ARCHITECTURE or bool(
            re.search(r"\b(public api|schema|migration|concurrency|persistence)\b", request, re.I)
        )
        tests_mentioned = bool(re.search(r"\b(test|pytest|validation|mypy|ruff)\b", request, re.I))
        test_evidence = any("test" in path.lower() for path in paths)

        scope = min(100.0, 12.0 + len(words) * 0.55 + len(paths) * 8.0 + (30.0 if broad else 0.0))
        ambiguity = max(
            0.0,
            min(
                100.0,
                12.0
                + uncertain * 22.0
                + (25.0 if len(words) < 4 else 0.0)
                - min(len(paths) * 5.0, 25.0),
            ),
        )
        architecture_score = min(
            100.0, (70.0 if architecture else 10.0) + max(0, len(paths) - 1) * 5.0
        )
        verification = min(
            100.0,
            25.0
            + (25.0 if not tests_mentioned else 0.0)
            + (20.0 if evidence and not test_evidence else 0.0)
            + len(paths) * 3.0,
        )
        security_score = (
            85.0
            if security
            else (35.0 if re.search(r"\b(config|permission|token)\b", request, re.I) else 5.0)
        )
        uncertainty = min(
            100.0, 20.0 + ambiguity * 0.45 + (20.0 if not evidence and stage == "revised" else 0.0)
        )

        dimensions = {
            "scope": scope,
            "ambiguity": ambiguity,
            "architecture": architecture_score,
            "verification": verification,
            "security": security_score,
            "uncertainty": uncertainty,
        }
        complexity = sum(
            dimensions[key] * self.config.complexity_weights.get(key, 0.0) for key in dimensions
        )
        complexity = max(complexity, security_score * 0.75, architecture_score * 0.65)
        confidence = min(0.95, category_confidence + min(len(evidence), 5) * 0.04)

        risks: list[str] = []
        if security_score >= 60:
            risks.append("Security-sensitive behavior is involved")
        if architecture_score >= 60:
            risks.append("Public interfaces or architecture may be affected")
        if len(paths) > 3:
            risks.append("Relevant behavior spans multiple files")
        if evidence and not test_evidence:
            risks.append("No directly associated tests were found")
        if ambiguity >= 55:
            risks.append("Request scope is ambiguous")

        tier = self._tier(complexity, security_score, confidence)
        signals = {key: round(value, 2) for key, value in dimensions.items()}
        signals.update(
            {"relevant_files": float(len(paths)), "relevant_symbols": float(len(symbols))}
        )
        return TaskProfile(
            stage=stage,
            task_category=category,
            complexity_score=round(min(complexity, 100.0), 2),
            confidence=round(confidence, 3),
            scope_score=round(scope, 2),
            ambiguity_score=round(ambiguity, 2),
            architectural_impact_score=round(architecture_score, 2),
            verification_difficulty_score=round(verification, 2),
            security_risk_score=round(security_score, 2),
            context_uncertainty_score=round(uncertainty, 2),
            risk_factors=risks,
            relevant_signals=signals,
            recommended_capability_tier=tier,
            evidence_paths=paths,
            evidence_symbols=symbols,
        )

    @staticmethod
    def _category(request: str) -> tuple[TaskCategory, float]:
        matches = [category for category, pattern in _CATEGORY_PATTERNS if pattern.search(request)]
        if matches:
            return matches[0], 0.78 if len(matches) == 1 else 0.66
        if len(request.split()) <= 8 and re.search(
            r"\b(change|update|rename|remove)\b", request, re.I
        ):
            return TaskCategory.SMALL_EDIT, 0.62
        return TaskCategory.UNKNOWN, 0.35

    @staticmethod
    def _tier(complexity: float, security: float, confidence: float) -> CapabilityTier:
        if security >= 70 or complexity >= 80 or confidence < 0.45:
            return CapabilityTier.FRONTIER
        if complexity >= 60:
            return CapabilityTier.ADVANCED
        if complexity >= 32:
            return CapabilityTier.STANDARD
        return CapabilityTier.BASIC


class CodeKnowledgeBase:
    """Local lexical/structural retrieval over the repository index.

    Semantic retrieval is deliberately represented as a zero score unless an optional
    implementation is configured in a future release; lexical and AST symbol evidence
    remain fully functional offline.
    """

    def __init__(self, repository: Repository, config: IntelligenceConfig) -> None:
        self.repository = repository
        self.config = config

    def retrieve(self, request: str) -> list[RetrievalItem]:
        evidence = self.repository.search(request, limit=self.config.maximum_retrieved_files)
        results: list[RetrievalItem] = []
        token_budget = self.config.context_token_budget
        consumed = 0
        for item in evidence:
            token_cost = max(1, len(item.excerpt) // 4)
            if consumed + token_cost > token_budget:
                break
            consumed += token_cost
            symbol = item.matched_symbols[0] if item.matched_symbols else None
            graph_score = min(1.0, len(item.matched_symbols) * 0.25)
            lexical_score = min(1.0, item.score / 20.0)
            final = lexical_score * self.config.retrieval_weights.get(
                "lexical", 0.65
            ) + graph_score * self.config.retrieval_weights.get("graph", 0.35)
            results.append(
                RetrievalItem(
                    path=item.path,
                    symbol=symbol,
                    line_range=(item.start_line, item.end_line),
                    content_excerpt=item.excerpt,
                    retrieval_reasons=item.ranking_reasons,
                    lexical_score=round(lexical_score, 4),
                    semantic_score=0.0,
                    graph_score=round(graph_score, 4),
                    final_score=round(final, 4),
                    estimated_token_cost=token_cost,
                )
            )
        return sorted(results, key=lambda item: (-item.final_score, item.path, item.line_range[0]))
