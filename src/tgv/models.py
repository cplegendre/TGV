from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class StrictConfigModel(BaseModel):
    """Configuration model that rejects unknown keys instead of silently ignoring typos."""

    model_config = ConfigDict(extra="forbid")


def _validate_regex(value: str, *, field_name: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression for {field_name}: {exc}") from exc
    return value


class Route(StrEnum):
    LOCAL_FAST = "local_fast"
    LOCAL_CODE = "local_code"
    FRONTIER_STANDARD = "frontier_standard"
    FRONTIER_DEEP = "frontier_deep"
    # Backward-compatible alias for pre-0.22 callers and stored decisions.
    FRONTIER = "frontier_deep"


class ReasoningLevel(StrEnum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    HIGH = "high"


class TaskCategory(StrEnum):
    DOCUMENTATION = "documentation"
    FORMATTING = "formatting"
    SMALL_EDIT = "small_edit"
    REFACTOR = "refactor"
    BUG_FIX = "bug_fix"
    FEATURE = "feature"
    TESTING = "testing"
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    PERFORMANCE = "performance"
    DEPENDENCY_CHANGE = "dependency_change"
    REPOSITORY_INVESTIGATION = "repository_investigation"
    UNKNOWN = "unknown"


class CapabilityTier(StrEnum):
    BASIC = "basic"
    STANDARD = "standard"
    ADVANCED = "advanced"
    FRONTIER = "frontier"


class HandoffMode(StrEnum):
    HANDOFF_ONLY = "handoff-only"
    COMMAND = "command"
    DISABLED = "disabled"


class ModelRoleConfig(StrictConfigModel):
    provider: str
    model: str
    reasoning: ReasoningLevel = ReasoningLevel.STANDARD
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    maximum_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.25, ge=0, le=30)
    temperature: float = Field(default=0.0, ge=0, le=2)


class PatternConfig(StrictConfigModel):
    high_risk: dict[str, str] = Field(
        default_factory=lambda: {
            "security-sensitive": r"\b(vulnerability|exploit|rotate (?:production )?(?:credentials|keys|secrets)|redesign (?:authentication|authorization|security)|change auth(?:entication|orization)|(?:add|change|fix|implement|redesign|harden|rotate).{0,50}(?:auth(?:entication|orization)?|permission|csrf|xss|secret|credential|security))\b",  # noqa: E501
            "architecture": r"\b(redesign|re-architect|split(?: the| this| our)? (?:system )?into microservices|major migration|architecture migration|security architecture|(?:design|introduce|migrate|replace|split).{0,50}(?:architecture|microservices?|distributed system|public api|schema))\b",  # noqa: E501
            "concurrency": r"\b(race condition|deadlock|make .*thread[- ]?safe|concurrency bug)\b",
            "destructive": r"\b(delet(?:e|ing) production|drop (?:the )?(?:production )?database|force push|rotate keys)\b",  # noqa: E501
        }
    )
    code: str = (
        r"\b(add|fix|implement|refactor|write "
        r"(?:focused |regression |integration |end-to-end )?tests?|change|patch|debug|"
        r"optimi[sz]e|upgrade|downgrade|update (?=[^\n]{0,60}\b(?:dependenc\w*|lockfile|package|"
        r"pyproject|pytest|ruff|mypy|mcp)\b)|extract|restructure|support|reduce|benchmark|"
        r"improve (?=[^\n]{0,60}\b(?:latency|throughput|performance|memory|startup)\b))\b"
    )
    simple: str = r"\b(where|which file|explain|summari[sz]e|find|locate|inspect|what is)\b"

    @field_validator("code", "simple")
    @classmethod
    def validate_pattern(cls, value: str, info: ValidationInfo) -> str:
        return _validate_regex(value, field_name=info.field_name or "pattern")

    @field_validator("high_risk")
    @classmethod
    def validate_high_risk_patterns(cls, value: dict[str, str]) -> dict[str, str]:
        for name, pattern in value.items():
            _validate_regex(pattern, field_name=f"high_risk.{name}")
        return value


class TelemetryConfig(StrictConfigModel):
    enabled: bool = False
    log_path: Path = Path(".tgv/sessions")


class RoutingConfig(StrictConfigModel):
    use_local_model: bool = True
    local_answer_confidence: float = Field(default=0.82, ge=0, le=1)
    local_code_confidence: float = Field(default=0.68, ge=0, le=1)
    maximum_tool_rounds: int = Field(default=6, ge=1, le=20)
    force_frontier_for_high_risk: bool = True
    patterns: PatternConfig = Field(default_factory=PatternConfig)


class RepositoryConfig(StrictConfigModel):
    maximum_file_bytes: int = Field(default=524_288, ge=1024)
    maximum_results: int = Field(default=20, ge=1, le=200)
    excerpt_lines: int = Field(default=120, ge=10, le=1000)
    ignored_directories: list[str] = Field(
        default_factory=lambda: [
            ".git",
            ".tgv",
            ".venv",
            "node_modules",
            "dist",
            "build",
            "__pycache__",
        ]
    )
    ignored_suffixes: list[str] = Field(
        default_factory=lambda: [".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".pyc"]
    )
    index_enabled: bool = True
    index_path: Path = Path(".tgv/index.json")


class PrivacyConfig(StrictConfigModel):
    deny_patterns: list[str] = Field(
        default_factory=lambda: [
            "**/.env",
            "**/.env.*",
            "**/.npmrc",
            "**/.pypirc",
            "**/*.pem",
            "**/*.key",
            "**/id_rsa*",
            "**/id_ed25519*",
            "**/credentials*",
            "**/*credential*",
            "**/*token*",
            "**/*secret*",
        ]
    )


class HandoffConfig(StrictConfigModel):
    mode: HandoffMode = HandoffMode.HANDOFF_ONLY
    command: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    provider_name: str = "custom"
    result_path: Path | None = None

    @model_validator(mode="after")
    def validate_command(self) -> HandoffConfig:
        if self.mode == HandoffMode.COMMAND and not self.command:
            raise ValueError("handoff.command is required when mode is 'command'")
        if any("\x00" in part for part in self.command):
            raise ValueError("handoff.command arguments may not contain NUL bytes")
        return self


class IntelligenceConfig(StrictConfigModel):
    enabled: bool = True
    outcome_database_path: Path = Path(".tgv/outcomes.sqlite3")
    retain_raw_prompts: bool = False
    scoreboard_minimum_samples: int = Field(default=5, ge=1, le=10000)
    maximum_retrieved_files: int = Field(default=12, ge=1, le=100)
    maximum_retrieved_symbols: int = Field(default=24, ge=1, le=500)
    context_token_budget: int = Field(default=8000, ge=256, le=200000)
    semantic_retrieval_enabled: bool = False
    complexity_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "scope": 0.22,
            "ambiguity": 0.14,
            "architecture": 0.20,
            "verification": 0.18,
            "security": 0.18,
            "uncertainty": 0.08,
        }
    )
    retrieval_weights: dict[str, float] = Field(
        default_factory=lambda: {"lexical": 0.65, "semantic": 0.0, "graph": 0.35}
    )
    utility_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "success": 100.0,
            "quality": 20.0,
            "acceptance": 20.0,
            "cost": 4.0,
            "latency": 0.05,
            "rework": 25.0,
            "retry": 8.0,
            "safety": 1000.0,
        }
    )


class TGVConfig(StrictConfigModel):
    version: int = 1
    models: dict[str, ModelRoleConfig]
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    repository: RepositoryConfig = Field(default_factory=RepositoryConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    intelligence: IntelligenceConfig = Field(default_factory=IntelligenceConfig)


class RouteDecision(BaseModel):
    route: Route
    confidence: float = Field(ge=0, le=1)
    selected_model: str | None = None
    selected_capability_tier: CapabilityTier | None = None
    rationale: str
    risk_signals: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    suggested_validation: list[str] = Field(default_factory=list)
    matched_category: str | None = None
    matched_pattern: str | None = None
    decision_source: str = "deterministic"
    matched_signals: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    alternative_routes: dict[str, float] = Field(default_factory=dict)
    task_profile: TaskProfile | None = None
    scoreboard_recommendation: ScoreboardRecommendation | None = None


class TaskProfile(BaseModel):
    """Explainable 0-100 task-complexity profile with 0-1 confidence."""

    stage: str = "initial"
    task_category: TaskCategory
    complexity_score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    scope_score: float = Field(ge=0, le=100)
    ambiguity_score: float = Field(ge=0, le=100)
    architectural_impact_score: float = Field(ge=0, le=100)
    verification_difficulty_score: float = Field(ge=0, le=100)
    security_risk_score: float = Field(ge=0, le=100)
    context_uncertainty_score: float = Field(ge=0, le=100)
    risk_factors: list[str] = Field(default_factory=list)
    relevant_signals: dict[str, float] = Field(default_factory=dict)
    recommended_capability_tier: CapabilityTier
    evidence_paths: list[str] = Field(default_factory=list)
    evidence_symbols: list[str] = Field(default_factory=list)


class OutcomeTask(BaseModel):
    task_id: str
    timestamp: datetime
    repository_id: str
    request_hash: str
    task_category: TaskCategory
    initial_complexity_score: float | None = None
    revised_complexity_score: float | None = None
    routing_confidence: float | None = None
    selected_model: str | None = None
    selected_capability_tier: CapabilityTier | None = None
    routing_reasons: list[str] = Field(default_factory=list)
    retrieved_files: list[str] = Field(default_factory=list)
    retrieved_symbols: list[str] = Field(default_factory=list)
    context_token_estimate: int | None = None
    execution_status: str = "pending"
    tests_passed: bool | None = None
    static_checks_passed: bool | None = None
    human_accepted: bool | None = None
    rework_required: bool | None = None
    retry_count: int = 0
    latency_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    failure_category: str | None = None
    safety_violation: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionAttempt(BaseModel):
    """One durable execution attempt attributed to an outcome task."""

    attempt_id: str
    task_id: str
    repository_id: str
    routing_decision_id: str | None = None
    attempt_number: int = Field(ge=1)
    started_at: datetime
    completed_at: datetime | None = None
    status: str = "pending"


class AgentSessionLink(BaseModel):
    """Opaque reference to an external agent session, never its contents."""

    session_link_id: str
    attempt_id: str
    agent: str
    external_session_id: str
    content_hash: str | None = None
    linked_at: datetime


class TerMeasurement(BaseModel):
    """Aggregate efficiency metrics linked to one attempt; no TER internals."""

    measurement_id: str
    attempt_id: str
    session_link_id: str | None = None
    analyzer: str
    analyzer_version: str
    measured_at: datetime
    metrics: dict[str, float | int | None] = Field(default_factory=dict)


class CandidateScore(BaseModel):
    model: str
    capability_tier: CapabilityTier
    sample_size: int
    success_probability: float = Field(ge=0, le=1)
    expected_cost: float = Field(ge=0)
    expected_latency: float = Field(ge=0)
    utility_score: float
    confidence: float = Field(ge=0, le=1)
    components: dict[str, float] = Field(default_factory=dict)


class ScoreboardRecommendation(BaseModel):
    recommended_model: str | None = None
    recommended_capability_tier: CapabilityTier | None = None
    expected_success_probability: float | None = None
    expected_cost: float | None = None
    expected_latency: float | None = None
    utility_score: float | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    sample_size: int = 0
    alternative_candidates: list[CandidateScore] = Field(default_factory=list)
    explanation: str
    fallback_used: bool = False
    fallback_reason: str | None = None


class RetrievalItem(BaseModel):
    path: str
    symbol: str | None = None
    line_range: tuple[int, int]
    content_excerpt: str
    retrieval_reasons: list[str] = Field(default_factory=list)
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    graph_score: float = 0.0
    final_score: float = 0.0
    estimated_token_cost: int = 0


class BenchmarkCaseResult(BaseModel):
    case_id: str
    category_correct: bool
    complexity_band_correct: bool
    route_acceptable: bool
    required_file_recall: float
    required_symbol_recall: float
    forbidden_file_exposure: int


class CodeSymbol(BaseModel):
    name: str
    kind: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    parent: str | None = None


class IndexDiagnostics(BaseModel):
    files_indexed: int = 0
    files_skipped: int = 0
    symbols_extracted: int = 0
    build_seconds: float = 0.0
    search_seconds: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0


class Evidence(BaseModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    reason: str
    excerpt: str
    score: float = 0.0
    matched_terms: list[str] = Field(default_factory=list)
    matched_symbols: list[str] = Field(default_factory=list)
    ranking_reasons: list[str] = Field(default_factory=list)


class ProviderLifecycle(BaseModel):
    provider_name: str
    mode: HandoffMode
    command: list[str] = Field(default_factory=list)
    dry_run: bool = False
    started_at: str
    completed_at: str | None = None
    exit_code: int | None = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    result_path: str | None = None
    result_summary: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    suggested_next_steps: list[str] = Field(default_factory=list)
    command_status: str = "not-run"


class OmittedSummary(BaseModel):
    ignored: int = 0
    denied: int = 0
    too_large: int = 0
    binary_or_encoding: int = 0
    unsafe_symlink: int = 0


class ContextBundle(BaseModel):
    schema_version: int = 2
    request: str
    repository_root: str
    decision: RouteDecision
    evidence: list[Evidence]
    handoff_markdown: str
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    omitted_files: OmittedSummary = Field(default_factory=OmittedSummary)
    validation_plan: list[str] = Field(default_factory=list)
    recommended_next_action: str = (
        "Review evidence, verify assumptions, then make the smallest safe change."
    )
    repository_summary: RepositorySummary | None = None
    provider: ProviderLifecycle | None = None


class RepositorySummary(BaseModel):
    root: Path
    file_count: int
    sampled_extensions: dict[str, int]
    indexed_file_count: int = 0


class OutcomeRecord(BaseModel):
    """Privacy-minimal future outcome metadata; never stores repository content."""

    session_id: str
    succeeded: bool | None = None
    rework_required: bool | None = None
    estimated_cost: float | None = Field(default=None, ge=0)
