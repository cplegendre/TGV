# Roadmap

## 0.1 — Foundation

- Stable package layout and public interfaces.
- Offline routing and optional Ollama integration.
- Read-only repository tools.
- Evidence-backed context bundles.
- Optional MCP server.

## 0.1 — Measurable routing

- Task outcome records and cost fields.
- Benchmark fixtures for routing accuracy.
- Hardware-aware model profiles.
- TER-compatible session and evaluation export.
- Configurable privacy classification and redaction hooks.

## 0.2 — Repository intelligence

- Incremental index.
- Syntax-tree and symbol-aware retrieval.
- Test-to-source relationships.
- Git diff and change-aware context selection.

## 0.3 — Agent integrations

- Claude Code launcher/adapter.
- Codex launcher/adapter.
- OpenAI-compatible local endpoint adapter.
- Approval policies for write tools.

## Non-goals for early releases

- Replacing frontier models for every task.
- Automatically executing arbitrary shell commands.
- Claiming savings without task-success measurements.
- Acting as a hardened sandbox or data-loss-prevention product.

## Delivered in 0.15

- Explainable scoring router with deterministic high-risk override.
- Incremental local lexical index and stable evidence ranking.
- Context bundle schema v2.
- Optional safe command-provider handoff.
- Expanded CI and privacy defaults.

## Delivered in 0.30

- Strict configuration and startup-time regex validation.
- Enforceable daemon deadlines on POSIX.
- Explicit selected-model/capability-tier execution plans.
- Repository-scoped adaptive routing and stricter success evidence.
- Read-only MCP persistence mode.
- Pruned repository walking, SQLite contention hardening, bounded subprocess diagnostics, and command-secret redaction.
## Delivered in 0.31

- Cross-platform process-isolated daemon routing with restart-on-timeout cancellation.
- Worker readiness handshake, heartbeat health checks, operational metrics, and privacy-minimal diagnostics.
- 104-case integrated intelligence corpus with CI-enforced accuracy/risk thresholds.
- Provider latency/error accounting and production operations guidance.


## Delivered in 0.32

- Graceful daemon draining with configurable shutdown grace and in-flight worker cancellation.
- Prometheus-compatible file metrics plus lifecycle states and cancellation counters.
- Real outcome-ledger evaluation with enforceable evidence/success/safety thresholds.
- Counterfactual routing explanations through `tgv explain`.
- Atomic owner-only local state for indexes, status/metrics, telemetry, and context bundles on supported filesystems.
