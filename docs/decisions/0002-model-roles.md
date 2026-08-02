# ADR 0002: Semantic model roles and reasoning budgets

- Status: Accepted
- Date: 2026-07-29

## Decision

TGV defines four semantic execution roles: `fast`, `code`, `frontier_standard`, and `frontier_deep`. Each role configures a provider, model, and provider-neutral reasoning budget (`minimal`, `standard`, or `high`).

The corresponding routes are `local_fast`, `local_code`, `frontier_standard`, and `frontier_deep`.

## Rationale

Four roles distinguish two separate concerns that a three-tier design combined:

- a clear task that needs frontier capability but not extended reasoning;
- an ambiguous, risky, architectural, or high-impact task that warrants a larger reasoning budget.

Semantic roles allow hardware-specific profiles and future Ollama, MLX, llama.cpp, OpenAI-compatible, or coding-agent adapters without changing routing logic. Reasoning budget is configuration rather than a vendor-specific mode name.

## Compatibility

The legacy `Route.FRONTIER` enum member remains as an alias for `frontier_deep` for Python callers. Configuration should use the explicit `frontier_standard` and `frontier_deep` role names from 0.22 onward.
