# TGV

**Task Gateway and Validator**  
*Local first. Cloud when necessary.*

TGV is an experimental local-first gateway for AI coding agents. It inspects a task, gathers repository evidence, decides whether the request can be handled locally, and prepares a compact handoff when a frontier model is warranted.

> [!WARNING]
> Version 0.29 is a production-hardening pre-1.0 release with explainable routing, confined incremental repository indexing, Ollama integration, deterministic context bundles, optional external-agent handoff, telemetry, and an optional MCP server. It is not yet a production security boundary.

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

Only the latest published minor line is supported. During the current `0.x` phase, upgrade to **0.29** for fixes. See [SECURITY.md](SECURITY.md).

## v0.29 capabilities

- Four explainable routing profiles: `local_fast`, `local_code`, `frontier_standard`, and `frontier_deep`.
- Provider-neutral reasoning budgets: `minimal`, `standard`, and `high`.
- Direct safety escalation to `frontier_deep` for security, architecture, concurrency, destructive, and highly ambiguous work.
- `frontier_standard` routing for clear, substantial multi-file tasks that exceed the local coding profile.
- Offline heuristic routing when Ollama is unavailable.
- Configurable Ollama, OpenAI-compatible, and external model roles.
- Ollama and OpenAI-compatible chat adapters with validated configuration, explicit timeouts, retries, and structured JSON output.
- Repository-safe retrieval, deterministic context bundles, optional command handoff, telemetry, benchmarks, and an optional MCP server.

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

The v0.29 server exposes read-only tools:

- `list_repository_files`
- `search_repository`
- `read_file_excerpt`
- `prepare_context_bundle`

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
confidence, matched category and pattern, rationale, and risk signals. TGV explicitly
excludes repository file contents, excerpts, context bundles, and secrets from telemetry.

Install the optional TER integration before scoring sessions:

```bash
python -m pip install -e ".[ter]"
```

### Report efficiency by route

```bash
tgv efficiency --sessions .tgv/sessions
tgv efficiency --sessions .tgv/sessions --format json
```

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
tgv doctor --config config/tgv.example.yaml
```

Explain output includes the active confidence thresholds, deterministic/model decision source, matched policy, and risk signals. `doctor` reports configuration status, Ollama availability, the configured fast model, telemetry state, and only the count of deny patterns.

Repository search is currently a bounded full scan. A postings-based index is planned; path containment, file-size limits, deny patterns, and result limits remain enforced.

When `tgv calibrate --apply` writes YAML, it serializes the validated configuration model. Key order and comments may therefore change. Dry-run remains the default.

## 0.29 production hardening

TGV works offline by default. Repository search uses a local incremental lexical index and falls back to a bounded scan if indexing fails. Common secret-bearing files are denied by default.

```bash
tgv route "Implement a bounded parser fix" --repo . --json
tgv search "RouteDecision confidence" --repo . --json
tgv prepare "Fix the parser" --repo . --output .tgv/handoff --mode handoff-only
tgv prepare "Fix the parser" --repo . --output .tgv/handoff --mode command --dry-run --json
```

For command mode, configure an argument array; placeholders `{handoff_markdown}` and `{bundle_json}` are expanded without shell interpolation. TGV reduces accidental exposure but is not a full security boundary.


## 0.29 quality and security hardening

TGV 0.29 enforces repository-root confinement for user paths, symlinks, and index files; includes a real loopback Ollama adapter integration test; and raises the branch-aware coverage gate to 98%. Run `PYTHONPATH=src python scripts/bench_efficiency.py --repo .` for a deterministic local estimate of frontier-token and cost savings. See `docs/dependency-audit.md` for audit scope and limitations. TGV reduces accidental disclosure risk but is not a complete security boundary.

### Quality diagnostics

- `tgv bench routing [--json]` evaluates the committed 76-case labeled routing corpus.
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

TGV 0.29 adds two-stage task profiling, bounded lexical/structural retrieval, a privacy-minimal SQLite outcome ledger, and a statistically guarded model-routing scoreboard. Historical recommendations activate only after the configured minimum sample count; otherwise TGV reports the evidence gap and keeps the deterministic route. Mandatory safety escalation always wins.

```bash
tgv profile "Fix refresh-token rotation" --repo . --json
tgv retrieve "AuthenticationService" --repo . --json
tgv scoreboard "Fix refresh-token rotation" --repo . --json
tgv outcomes list --repo .
tgv benchmark run --json
```

See `docs/routing-intelligence.md` for formulas, privacy behavior, architecture, limitations, and configuration.
