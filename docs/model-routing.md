# Model routing

TGV 0.22 routes software-engineering requests across four provider-neutral capability profiles:

| Route | Intended workload | Default reasoning budget |
|---|---|---|
| `local_fast` | Classification, retrieval planning, repository lookup, summaries, and trivial operations | `minimal` |
| `local_code` | Bounded implementation, localized fixes, tests, and routine refactoring | `standard` |
| `frontier_standard` | Clear, substantial, multi-file work that exceeds the local coding profile | `standard` |
| `frontier_deep` | Ambiguous, architectural, high-risk, security-sensitive, destructive, or repeatedly failing work | `high` |

The router extracts deterministic signals including extractive intent, bounded coding work, repository scale, scope ambiguity, destructive or security-sensitive actions, explicit constraints, and uncertainty. It records scores, alternatives, rationale, confidence, matched signals, and the decision source.

High-risk matches remain a policy override. When `force_frontier_for_high_risk` is enabled, security, architecture, concurrency, and destructive signals route directly to `frontier_deep`. This bypasses lower tiers rather than requiring sequential escalation.

A configured local routing model may replace the deterministic result only when it clears the relevant confidence threshold. Provider errors fall back to deterministic routing.

## Capability profiles, not hard-coded models

Routes do not identify vendors. Configuration maps each role to a provider, model, endpoint, and reasoning budget. This allows a team to replace an Ollama model or frontier API without changing routing policy.

Parameter count is not used as the primary routing criterion. Model quality, active parameters, context capacity, tool use, latency, hardware constraints, task risk, and validation strength are more useful signals.

## Execution scope

TGV 0.22 selects profiles and prepares handoffs. Local routing uses the configured Ollama-compatible provider. External execution remains adapter-driven through the existing command handoff interface; provider-specific autonomous execution, retries, tool normalization, and validation loops remain future work.
