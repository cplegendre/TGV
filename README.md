# TGV

**Task Gateway and Validator**  
*Local first. Cloud when necessary.*

TGV is an experimental local-first gateway for AI coding agents. It inspects a task, gathers repository evidence, decides whether the request can be handled locally, and prepares a compact handoff when a frontier model is warranted.

> [!WARNING]
> Version 0.32 is a production-candidate pre-1.0 release with graceful draining, Prometheus-compatible metrics export, real outcome-ledger evaluation, counterfactual route explanations, private atomic state files, cross-platform worker cancellation, and the CI-enforced 104-case intelligence benchmark. It is not yet a hardened network security boundary.

## Try TGV locally

Run the fully local synthetic demo:

```bash
tgv --demo
```

The demo exercises deterministic routing, telemetry, and reporting without requiring an API key, Ollama, network access, or optional dependencies.

All demo repositories, tasks, telemetry, savings figures, and reports are synthetic illustrations. They are not evidence of production performance or savings. The default 450-task corpus is processed with a compact progress bar and a summary-only terminal result. Extended deterministic runs are available with `tgv demo --scale medium` (5,000), `--scale large` (25,000), `--scale stress` (100,000), or `--tasks N`. Expanded runs cycle the canonical corpus and measure throughput; they are not additional independent benchmark cases or evidence of production quality.


## Why TGV?

Not every software-engineering task requires the most expensive or capable AI model. TGV evaluates each request and routes it to the most appropriate capability profile:

1. **Local Fast** — classification, repository navigation, extraction, summaries, and trivial operations.
2. **Local Code** — bounded implementation, localized fixes, tests, and routine refactoring on a capable local coding model.
3. **Frontier Standard** — clear multi-file work executed by an external coding model or agent with a normal reasoning budget.
4. **Frontier Deep** — ambiguous, architectural, security-sensitive, high-impact, or repeatedly failing work using an extended reasoning budget.

This can reduce API cost, keep more source code local, improve latency for routine work, standardize model selection, and reserve frontier capacity for tasks that genuinely require it. TGV does not replace frontier coding agents; it selects when to use them and prepares focused repository evidence for the handoff.

TGV routes by complexity, risk, privacy requirements, expected cost, and available validation—not parameter count alone. Model names remain configuration so teams can adapt the four profiles to their hardware and providers.

## Supported versions

Only the latest published minor line is supported. During the current `0.x` phase, upgrade to **0.32** for fixes. See [SECURITY.md](SECURITY.md).

## v0.32 capabilities

- Four explainable routing profiles: `local_fast`, `local_code`, `frontier_standard`, and `frontier_deep`.
- Provider-neutral reasoning budgets: `minimal`, `standard`, and `high`.
- Direct safety escalation to `frontier_deep` for security, architecture, concurrency, destructive, and highly ambiguous work.
- `frontier_standard` routing for clear, substantial multi-file tasks that exceed the local coding profile.
- Offline heuristic routing when Ollama is unavailable.
- Configurable Ollama, OpenAI-compatible, and external model roles.
- Ollama and OpenAI-compatible chat adapters with validated configuration, explicit timeouts, retries, and structured JSON output.
- Repository-safe retrieval, deterministic context bundles, optional command handoff, telemetry, benchmarks, and an optional MCP server.
- Strict configuration: unknown keys and malformed routing regexes fail during startup.
- Explicit execution plans expose the selected model and capability tier for every route.
- Repository-scoped adaptive recommendations; unverified completed outcomes are not counted as successes.
- Cross-platform hard request deadlines for the stdio daemon using an isolated persistent worker process; timed-out workers are terminated before the timeout is returned.
- MCP read-only mode suppresses repository-index, outcome-ledger, and telemetry persistence.
- Ignored-directory pruning, SQLite WAL/busy-timeout hardening, bounded subprocess capture, and secret-flag redaction.
- Worker readiness handshake plus heartbeat health checks through `tgv health`.
- Operational status metrics for requests, routes, worker restarts, routing latency, and local-provider latency/errors.
- Optional privacy-minimal request diagnostics JSONL for production troubleshooting.
- Graceful daemon draining: SIGTERM/SIGINT stop accepting new work, allow an in-flight request a configurable grace period, then terminate the isolated worker if necessary.
- Prometheus textfile export for daemon readiness, request counters, route counts, worker restarts, routing latency, and provider calls/errors.
- Counterfactual route explanations through `tgv explain`: why the route won and what request/history change could make another route win.
- Outcome-ledger evaluation through `tgv benchmark outcomes`, including verified success, human acceptance, rework, safety, retry, latency, and cost summaries by model/tier/category.
- Atomic owner-only operational files for status, metrics, indexes, telemetry, and context bundles on POSIX-compatible filesystems.
- 104-case integrated intelligence benchmark with a CI-enforced release-quality gate.

## Architecture

```text
Developer
   |
   v
TGV CLI / future daemon
   |-- Repository tools and policy
   |-- Local Fast model (Ollama)
   |-- Local Code model (Ollama)
   |-- Frontier Standard profile
   |-- Frontier Deep profile
   |-- Context compiler
   `-- External-agent handoff adapter
             |
             v
      Claude Code / Codex / other

TGV can also expose repository tools over MCP (stdio).
```

See [docs/architecture.md](docs/architecture.md) and [docs/decisions](docs/decisions/) for rationale.

## Quick start

The primary onboarding path is [docs/getting-started.md](docs/getting-started.md).


```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]"

cp config/tgv.example.yaml tgv.yaml
tgv doctor --config tgv.yaml
tgv inspect .
tgv route "Where is authentication configured?" --repo .
tgv explain "Fix refresh-token rotation" --repo .
tgv prepare "Fix refresh-token rotation" --repo . --output .tgv/bundles/auth
```

The core package works without the `mcp` extra. Local providers are optional; TGV falls back to deterministic routing if the configured endpoint is unavailable. Adaptive scoreboard and calibration features are experimental.

## Ollama model roles

The example profile uses:

```yaml
models:
  fast:
    provider: ollama
    model: qwen3:4b
    reasoning: minimal
  code:
    provider: ollama
    model: qwen3-coder:30b
    reasoning: standard
  frontier_standard:
    provider: external
    model: auto
    reasoning: standard
  frontier_deep:
    provider: external
    model: auto
    reasoning: high
```

These are recommendations, not requirements. A 7B coding model may be more appropriate on a 16 GB machine. TGV never downloads models automatically.

## MCP server

Install the MCP extra and launch:

```bash
pip install -e ".[mcp]"
tgv-mcp --repo .
```

Example Claude-style registration:

```json
{
  "mcpServers": {
    "tgv": {
      "command": "tgv-mcp",
      "args": ["--repo", "."]
    }
  }
}
```

The v0.32 server exposes read-only tools:

- `list_repository_files`
- `search_repository`
- `read_file_excerpt`
- `prepare_context_bundle`

MCP uses an explicit read-only service mode: these tools do not persist `.tgv/index.json`, outcome records, or telemetry.

## Development

```bash
make install
make check
make build
```

Or run individually:

```bash
pytest --cov=tgv
ruff check .
ruff format --check .
mypy src/tgv
python -m build
```

## Project status

This repository is intended as a clean foundation for experimentation. The next milestone should be driven by measured task outcomes, not model hype. See [ROADMAP.md](ROADMAP.md).

## Security and privacy

TGV is designed for local-first workflows, but context bundles can still contain sensitive source code. Review [SECURITY.md](SECURITY.md) and [docs/threat-model.md](docs/threat-model.md) before connecting it to hosted models.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Measuring routing efficiency

TGV integrates optionally with [TER](https://github.com/cplegendre/TER), the Token
Efficiency Ratio analyzer for Claude Code sessions. The integration answers a practical
question: are TGV's `local_fast`, `local_code`, `frontier_standard`, and `frontier_deep` routing decisions producing
efficient agent sessions, or are some routes accumulating avoidable token waste?

### Enable privacy-minimal telemetry

Telemetry is disabled by default. Enable it in `tgv.yaml` only when you want to collect
post-hoc calibration data:

```yaml
telemetry:
  enabled: true
  log_path: .tgv/sessions
```

Each recorded interaction contains the request text and routing metadata: route,
confidence, matched category and pattern, rationale, and risk signals. TGV does not add
repository file contents, excerpts, or context bundles to telemetry, but request text can
itself contain sensitive data; keep telemetry disabled unless that retention is acceptable.

Install the optional TER integration before scoring sessions:

```bash
python -m pip install -e ".[ter]"
```

This installs the verified TER 3.0.0 package with its `embeddings` extra.
The first real analysis may download an embedding model and requires network access
unless its model files are already cached. See [TER integration](docs/ter-integration.md)
for installation checks and limitations.

### Report efficiency by route

```bash
tgv efficiency --sessions .tgv/sessions
tgv efficiency --sessions .tgv/sessions --format json
```

Warning: TGV's built-in telemetry is synthetic routing narration, not a downstream
coding-agent transcript. TER scores the input JSONL, so these numbers are **not**
evidence of real coding-agent execution efficiency unless real session logs are supplied
and reliably associated with the route. Session linking is future work.

The report groups sessions by `local_fast`, `local_code`, `frontier_standard`, and `frontier_deep`, showing aggregate
TER, aligned tokens, waste tokens, and session count. Higher TER indicates a larger share
of scored tokens aligned with the request; the waste-token count identifies the volume of
scored output TER classified as avoidable.

### Calibrate routing thresholds

`tgv calibrate` applies a deliberately simple, explainable heuristic to the efficiency
report. It is a dry run unless `--apply` is supplied:

```bash
# Dry run using the defaults
tgv calibrate --sessions .tgv/sessions --config tgv.yaml

# Review and then write proposed thresholds
tgv calibrate --sessions .tgv/sessions --config tgv.yaml --apply
```

The tuning controls are:

- `--target-waste-ratio 0.26`: desired maximum waste share.
- `--minimum-sessions 5`: minimum route volume required before proposing a change.
- `--step-size 0.05`: amount by which a confidence threshold is raised or lowered.

When a local route's observed waste ratio exceeds the target, TGV proposes raising its
confidence threshold so fewer uncertain tasks stay local. When waste is low and the route
has sufficient volume, it proposes lowering the threshold slightly so more work can remain
local. Routes below the minimum session count are reported as insufficient data and are
not changed.

`--apply` rewrites the selected YAML file from TGV's validated configuration model. The
result remains semantically equivalent, but comments and key ordering may change.

### Explain routing and inspect local health

```bash
tgv route "Where is authentication configured?" --explain
tgv explain "Implement token rotation safely" --repo .
tgv doctor --config config/tgv.example.yaml
```

`tgv route --explain` exposes the raw routing evidence. `tgv explain` adds a human-oriented counterfactual trace: the strongest reasons for the selected route and estimated changes that would make each alternative route win. Explain output also includes the decision source, selected model/capability tier, task profile, historical evidence, matched policy, and risk signals. `doctor` reports configuration status, provider availability, the configured fast model, telemetry state, and only the count of deny patterns.

Repository search uses a bounded incremental lexical/AST index. Ignored directories are pruned before descent, while path containment, file-size limits, deny patterns, and result limits remain enforced.

When `tgv calibrate --apply` writes YAML, it serializes the validated configuration model. Key order and comments may therefore change. Dry-run remains the default.

## 0.32 operational readiness

Run the stdio gateway with heartbeat status and privacy-minimal diagnostics:

```bash
tgv serve --repo . \
  --status-file .tgv/status.json \
  --diagnostics-file .tgv/diagnostics.jsonl \
  --metrics-file .tgv/tgv.prom \
  --request-timeout 120 \
  --shutdown-grace-seconds 10

tgv health --status-file .tgv/status.json --max-age 15 --json
tgv metrics --status-file .tgv/status.json --format prometheus
```

The daemon starts one isolated routing worker with the cross-platform `spawn` lifecycle. It reports readiness only after the worker startup handshake completes. A timed-out request terminates the worker before returning the timeout; the next request starts a clean worker. SIGTERM/SIGINT moves readiness to draining, stops accepting new requests, gives the in-flight request the configured grace period, then terminates it if necessary. Status and optional Prometheus textfile metrics include request counters, cancellations, route counts, worker restarts, and bounded routing/provider latency/error metrics. See `docs/operations.md` for deployment guidance and limitations.

The integrated routing-intelligence corpus now contains 104 labeled cases across all 13 task categories. `tgv benchmark run --enforce --json` is a CI release gate requiring at least 100 cases, 90% category accuracy, 90% complexity-band accuracy, 95% acceptable-route accuracy, and zero false-low-risk cases.

For observed production evidence, `tgv benchmark outcomes --repo . --json` evaluates the privacy-minimal outcome ledger instead of synthetic labels. `--enforce` can require a minimum verified sample count, minimum verified success rate, and maximum safety-violation rate. An empty or undersampled ledger is reported as insufficient evidence rather than treated as success.

## 0.30 production hardening

TGV works offline by default. Repository search uses a local incremental lexical index and falls back to a bounded scan if indexing fails. Common secret-bearing files are denied by default.

```bash
tgv route "Implement a bounded parser fix" --repo . --json
tgv search "RouteDecision confidence" --repo . --json
tgv prepare "Fix the parser" --repo . --output .tgv/handoff --mode handoff-only
tgv prepare "Fix the parser" --repo . --output .tgv/handoff --mode command --dry-run --json
```

For command mode, configure an argument array; placeholders `{handoff_markdown}` and `{bundle_json}` are expanded without shell interpolation. TGV reduces accidental exposure but is not a full security boundary.


## 0.30 quality and security hardening

TGV 0.30 keeps repository-root confinement and the 98% branch-aware coverage gate, while adding strict configuration, real stdio deadlines, read-only MCP persistence rules, bounded subprocess output, secret-flag redaction, and repository-pruned enumeration. Run `PYTHONPATH=src python scripts/bench_efficiency.py --repo .` for deterministic local routing/token-allocation proxy metrics; these are not measured counterfactual savings. See `docs/dependency-audit.md` for audit scope and limitations. TGV reduces accidental disclosure risk but is not a complete security boundary.

### Quality diagnostics

- `tgv bench routing [--json]` evaluates the committed 76-case low-level deterministic routing corpus.
- `tgv benchmark run --enforce [--json]` evaluates the 104-case integrated task-category/complexity/routing corpus and enforces the release gate.
- `tgv benchmark outcomes --repo . [--enforce] [--json]` evaluates actual privacy-minimal outcome records by model, capability tier, and task category.
- `tgv bench index --repo . [--query QUERY] [--json]` reports index size, symbols, cache behavior, and latency.
- `python scripts/check_version.py` verifies release-version consistency.

Python repository search is code-aware: AST metadata boosts exact class, function, method, async-function, assignment, and import matches while preserving lexical fallback for syntax-invalid files.

### Optional coding-agent command presets

Commands are argument arrays and never use shell interpolation. Examples: Claude Code `command: ["claude", "--print", "{handoff_markdown}"]`; Codex `command: ["codex", "exec", "--", "{handoff_markdown}"]`; custom `command: ["agent", "--context", "{bundle_json}"]`. Configure `provider_name`, `timeout_seconds`, and optional `result_path`; none of these tools is a mandatory dependency.

## 0.29 lint and type-safety hardening

TGV 0.29 brings the codebase to a fully clean state under its own CI gates:
`ruff check .` and `ruff format --check .` pass with zero findings across
`src/`, `tests/`, and `scripts/` (previously ~145 unaddressed lint findings),
and `mypy --strict` passes with zero errors on `src/tgv` (previously untyped
MCP decorators and a dict-type bug in the `search` CLI command). `Router.WEIGHTS`
is now wrapped in `MappingProxyType` to prevent accidental mutation of shared
routing weights. No behavioral changes; all 62+ tests still pass at 99% coverage.

## Routing Intelligence and Evaluation

TGV 0.32 uses two-stage task profiling, bounded lexical/structural retrieval, a privacy-minimal SQLite outcome ledger, and a statistically guarded model-routing scoreboard. Historical recommendations are scoped to the current repository, activate only after the configured minimum sample count, and now expose/apply the recommended model in the route execution plan. Unverified completed outcomes are not treated as successful evidence. Mandatory safety escalation always wins.

```bash
tgv profile "Fix refresh-token rotation" --repo . --json
tgv retrieve "AuthenticationService" --repo . --json
tgv scoreboard "Fix refresh-token rotation" --repo . --json
tgv explain "Fix refresh-token rotation" --repo .
tgv outcomes list --repo .
tgv benchmark run --enforce --json
tgv benchmark outcomes --repo . --json
```

See `docs/routing-intelligence.md` for formulas, privacy behavior, architecture, limitations, and configuration.
