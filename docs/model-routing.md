# Model routing

For the complete routing-to-TER evidence workflow, see
[TGV and TER end-to-end workflow](tgv-ter-workflow.md).

TGV 0.32 routes software-engineering requests across four provider-neutral capability profiles:

| Route | Intended workload | Default reasoning budget |
|---|---|---|
| `local_fast` | Classification, retrieval planning, repository lookup, summaries, and trivial operations | `minimal` |
| `local_code` | Bounded implementation, localized fixes, tests, and routine refactoring | `standard` |
| `frontier_standard` | Clear, substantial, multi-file work that exceeds the local coding profile | `standard` |
| `frontier_deep` | Ambiguous, architectural, high-risk, security-sensitive, destructive, or repeatedly failing work | `high` |

The router extracts deterministic signals including extractive intent, bounded coding work, repository scale, scope ambiguity, destructive or security-sensitive actions, explicit constraints, and uncertainty. It records scores, alternatives, rationale, confidence, matched signals, and the decision source.

High-risk matches remain a policy override. When `force_frontier_for_high_risk` is enabled, security, architecture, concurrency, and destructive signals route directly to `frontier_deep`. This bypasses lower tiers rather than requiring sequential escalation.

A configured local routing model may replace the deterministic result only when it clears the relevant confidence threshold. Provider errors fall back to deterministic routing.

## Routing confidence semantics

`RouteDecision.confidence` describes confidence in a routing choice; it is not an established
probability that the task will pass verification. Deterministic routing uses route-specific
heuristic constants (0.78 for `local_fast`, 0.72 for `local_code`, 0.64 for
`frontier_standard`, and 0.55 for `frontier_deep`; high-risk routing uses 0.92).
Accepted model suggestions use provider-reported confidence. These values have different
origins and should not be interpreted as interchangeable success probabilities.

Scoreboard recommendations and safety overrides can change the final route and selected
model while retaining the baseline routing confidence. The scoreboard's candidate
confidence and expected success probability are separate fields. The outcome ledger stores
the routing decision's confidence, along with the authoritative final route and decision
source when recorded; it does not reconstruct the origin of retained confidence.

`tgv confidence --repo . --json` provides exploratory probability-style diagnostics using
that persisted value and verified task outcomes. Overall statistics explicitly pool mixed
populations; route, model, tier, source, category, and provenance groups help expose those
differences. Historical missing or invalid provenance remains visible and is never inferred
from capability tiers. Model and tier groups describe final selection, not confidence origin.
The command does not change routing, confidence constants, scoreboard rankings, or PR-07
application semantics. See [confidence diagnostics](benchmarks.md#confidence-diagnostics)
for formulas, coverage denominators, and limitations.

## Capability profiles, not hard-coded models

Routes do not identify vendors. Configuration maps each role to a provider, model, endpoint, and reasoning budget. This allows a team to replace an Ollama model or frontier API without changing routing policy.

Parameter count is not used as the primary routing criterion. Model quality, active parameters, context capacity, tool use, latency, hardware constraints, task risk, and validation strength are more useful signals.

## Execution scope

TGV 0.32 selects profiles and prepares handoffs. Local routing uses the configured Ollama-compatible provider. External execution remains adapter-driven through the existing command handoff interface; provider-specific autonomous execution, retries, tool normalization, and validation loops remain future work.

## Evidence-gated threshold calibration

Evidence mode provides observational diagnostics and conservative **no-change**
recommendations:

```bash
# Inspect evidence without writing configuration or migrating the ledger
tgv calibrate --evidence --repo . --config tgv.yaml --json

# Retained explicit interface; current historical-only recommendations do not write
tgv calibrate --evidence --repo . --config tgv.yaml --apply
```

`local_answer_confidence` and `local_code_confidence` control acceptance of model
suggestions for `local_fast` and `local_code`, respectively. They do not control local
provider eligibility. Rejecting a suggestion returns to deterministic fallback routing,
which may select the same route, another local route, or a less capable route. Scoreboard
ranking and safety overrides remain unchanged and do not use these acceptance thresholds.

High TER waste and a strong observed success rate do not establish what rejecting model
suggestions would do. Evidence mode has no numerical adjustment rule. It preserves each
configured threshold, reports `direction=none`, and sets `application_permitted=false`.
Even when every descriptive evidence check passes, the recommendation is no change.
There is no automatic application, new routing policy, or inferred causal benefit.

### Evidence and statistical semantics

The command resolves `intelligence.outcome_database_path` like the execution service:
relative paths are relative to `--repo` (or the current directory), and absolute paths
are used as configured. It reads persisted aggregates only, without invoking a provider,
reading session content, creating a database, repairing measurements, or migrating schemas.
Older database schemas require an explicit writable upgrade through an existing command.

Target populations require an authoritative local route and decision source `model`.
Deterministic, scoreboard, safety-override, frontier, missing, and invalid provenance are
excluded. Historical route/source metadata may be missing; it is never inferred.
Replacing routing information through the legacy API clears an omitted decision source.

The report separates:

- **Availability:** eligible tasks, verified outcomes, valid comparable measurements,
  and distinct tasks paired with both outcomes and measurements. Descriptive minimums
  are 20 verified outcomes, 20 measurements, and 20 paired tasks per target.
- **Correctness confidence:** verified successes divided by verified successes plus
  verified failures, following the existing outcome contract. Unverified and incomplete
  tasks do not enter that denominator. Verification coverage uses all eligible tasks.
  The 95% Wilson lower bound is compared with 0.90 at full floating-point precision;
  displayed interval values are rounded. A displayed 0.9000 can therefore accompany a
  failed gate when the actual bound is slightly below 0.90.
- **Efficiency observations:** one canonical valid measurement per session, then one
  globally selected analyzer/version scope shared by all groups. TER CLI 3.0.0 is the
  supported calibration scope. Invalid metrics are excluded and counted; totals must
  match aligned plus waste tokens when supplied. Valid historical measurements without
  totals remain supported. Aggregate TER is token-weighted; waste is waste tokens divided
  by total tokens. Measured zero-token populations have TER 1.0 and waste 0.0; absent
  measurements have null TER and null waste.
- **Intervention eligibility:** always false for the current historical-only evidence.
  Minimum samples and a passing correctness diagnostic are not proof of causal validity.

Task outcomes count once. Repeated attempts contribute session-level measurements and
token weight, not independent correctness evidence. Efficiency includes failed,
unverified, and incomplete attempts associated with eligible tasks. Pairing is at task
level, so a measured attempt need not be the attempt that ultimately succeeded. Report-wide
invalid measurement counts include excluded populations and unattributed measurements;
per-target counts cover invalid measurements attached to that target's eligible sessions.

Verification coverage, missing measurements, selection bias, task difficulty, model
selection, and historical policy changes limit interpretation. Wilson intervals describe
observed verified outcomes; they do not remove these biases. TER efficiency is separate
from verified correctness. Evidence mode does not calculate Brier score, ECE, or reliability
bins; `tgv confidence` provides those read-only diagnostics separately. Neither command performs
counterfactual execution analysis.

### Explicit application safety

`--apply` requires `--config`, rejects symbolic-link configuration files, and performs no
configuration write or lock-file creation when no eligible proposal exists. Current
evidence recommendations are all ineligible. The retained application helper is tested
with hypothetical eligible proposals separately from the evidence engine.

For an eligible proposal, application uses a stable adjacent advisory lock, validates the
complete configuration under that lock, and rechecks file identity and content immediately
before atomic replacement. Cooperating processes serialize; stale snapshots are rejected.
Ordinary permission bits and unrelated configuration values are preserved. YAML comments
and formatting may change. Temporary files are cleaned up on write/replacement failure;
the lock file intentionally remains so all processes use the same lock inode.

The writer requires POSIX advisory locking and fails closed if it is unavailable.
Non-cooperating external writers can still race the final check: advisory locking cannot
provide a filesystem compare-and-swap. Ownership, ACLs, extended attributes, and crash
recovery durability are not promised by ordinary permission preservation and atomic rename.

Evidence mode is separate from the unchanged legacy `--sessions` heuristic. It does not
accept `--sessions`, `--target-waste-ratio`, `--minimum-sessions`, or `--step-size`.
