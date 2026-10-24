# Getting started

TGV requires Python 3.11 or newer. The core installation needs no API key, Ollama instance, TER,
or other optional dependency. This page covers the first local setup; the complete linked-session
and TER measurement path is in [TGV and TER end-to-end workflow](tgv-ter-workflow.md).

## Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install tgv-agent
```

For a source checkout, install it editable instead:

```bash
cd /path/to/TGV
python -m pip install -e .
```

Optional extras:

| Extra | Purpose |
|---|---|
| `.[mcp]` | Read-only MCP server (`tgv-mcp`). |
| `.[ter]` | Pinned TER 3.0.0 CLI and embedding backend for linked-session measurement. |
| `.[dev]` | Test, lint, type-check, build, and audit tooling. |

## Check the installation

```bash
tgv --version
tgv --demo
```

The demo is fully local and synthetic; it requires no API key, Ollama, network access, TER, or
other optional dependency. Its default 450 tasks are shown through a compact terminal progress bar,
followed by aggregate PASS/FAIL, timing, throughput, and report metrics rather than one line per
task. Use `tgv demo --scale medium`, `--scale large`, or `--scale stress` for 5,000, 25,000, or
100,000 deterministic routes; `--tasks N` overrides the preset with any positive count. Expanded
runs cycle the canonical 450 tasks and are scalability demonstrations, not independent benchmark
corpora.

## Configure the environment

Start from the example configuration when a repository-scoped file is useful:

```bash
cp config/tgv.example.yaml tgv.yaml
tgv doctor --config tgv.yaml
```

The doctor report shows configuration status and availability for Ollama or an OpenAI-compatible
endpoint configured for the `fast` role. TGV falls back to deterministic routing when a configured
local endpoint is unavailable.

## Route a request

```bash
tgv route "Where is authentication configured?" --repo . --explain
```

High-risk security, destructive, concurrency, and architectural work escalates to `frontier_deep`.
Routing confidence describes confidence in the routing choice; it is not a calibrated probability
of verified success.

## Prepare a context bundle

```bash
tgv prepare "Fix refresh-token rotation" --repo . --output .tgv/bundles/auth --json
```

`prepare` routes the request, writes `bundle.json` and `handoff.md`, and creates a pending outcome
task when intelligence persistence is enabled. The task identifier is in the bundle assumptions as
`outcome_task_id=<uuid>`; the JSON response does not echo it. See
[prepare the execution context](tgv-ter-workflow.md#4-prepare-the-execution-context) for a shell
example that extracts it.

Pass `handoff.md` and `bundle.json` to an external coding agent. TGV does not automatically capture
or link that agent's session.

## Record an outcome

Record human acceptance or rejection after review:

```bash
tgv outcomes complete TASK_ID --accepted --repo .
# or
tgv outcomes complete TASK_ID --rejected --repo .
```

To associate a real Claude Code session and record attested checks:

```bash
tgv outcomes link TASK_ID \
  --session ~/.claude/projects/<project>/<session>.jsonl \
  --agent claude-code \
  --repo .
tgv outcomes validate TASK_ID --tests-passed --static-checks-passed --repo .
```

Codex `rollout-*.jsonl` files can be linked with `--agent codex`. This validates a stable
`session_meta` identity and nonempty user/assistant text, but linked Codex measurement is
unsupported until TER ships a validated Codex loader; `efficiency --linked` fails closed instead
of persisting a misleading result.

Linking stores the agent name, external session identifier, SHA-256 content hash, attempt
correlation, timestamps, and an absolute session file locator. It does not store raw transcript
contents, prompts, responses, or classified spans. The original session file remains at its
external path.

Validation flags record results the user attests to; TGV does not execute the tests or static
checks. Completion alone is not verified success: tests, static checks, or human feedback must also
be recorded. Completed tasks without evidence remain unverified and are excluded from verified
success rates.

The full workflow, including TER measurement and reports, continues in
[TGV and TER end-to-end workflow](tgv-ter-workflow.md).

## Enable telemetry

Telemetry is disabled by default. Enable it only when request-text retention is acceptable:

```yaml
telemetry:
  enabled: true
  log_path: .tgv/sessions
```

Telemetry includes request text and routing metadata. It does not include repository file contents,
excerpts, or context bundles.

## Generate a routing report

```bash
tgv report --sessions .tgv/sessions --json
```

The stable machine-readable fields `local_share`, `savings_tokens`, and `savings_ratio` are
routing/token-allocation proxies, not a measured counterfactual of frontier usage. The default
report uses coarse token estimates already recorded in telemetry.

The optional `--ter` flag scores the supplied JSONL with TER:

```bash
python -m pip install 'tgv-agent[ter]'
tgv report --sessions .tgv/sessions --ter --json
```

TGV's generated routing telemetry is synthetic and is **not** a transcript of downstream
coding-agent work. Running TER on that telemetry does not measure coding-agent execution
efficiency. For real sessions, link the session and use `tgv efficiency --linked` as described in
[the canonical workflow](tgv-ter-workflow.md).

## Inspect linked evidence

`tgv efficiency --linked` is a measurement operation: it revalidates each linked session, invokes
TER, and persists aggregate measurements in the outcome ledger. It is the only linked-evidence
command that writes.

Once measurements are persisted and outcomes are verified, the read-only evidence commands are:

```bash
tgv report --linked --repo . --json
tgv benchmark observed --repo . --json
tgv calibrate --evidence --repo . --json
tgv confidence --repo . --json
```

These reports never create, migrate, or repair the ledger. When the ledger is missing they return
an empty or insufficient-evidence result instead of writing a database, and they fail with a
controlled error for unsupported schema versions or unreadable records.

These commands have different inputs and minimum samples. See
[steps 8–13 of the workflow](tgv-ter-workflow.md#8-analyze-execution-efficiency-with-ter) before
interpreting their output. Confidence diagnostics do not require TER.

Outcome-based scoreboard recommendations are **experimental**. Until the configured minimum sample
count is met, TGV returns `fallback_used: true` with a reason and preserves the deterministic route.

## Start the stdio service

```bash
tgv serve --repo . \
  --status-file .tgv/status.json \
  --diagnostics-file .tgv/diagnostics.jsonl \
  --request-timeout 120
```

Send one JSON object per line:

```json
{"request":"Where is authentication configured?"}
```

Responses are written to stdout and structured request logs to stderr. Routing runs in an isolated
persistent worker process. The daemon is marked ready only after that worker completes its startup
handshake. If `--request-timeout` expires, the worker is terminated before the timeout is returned,
on every supported platform; the next request starts a clean worker. The status file is updated
atomically and includes heartbeat, request counters, route counts, worker restarts, and
routing/provider metrics. Optional diagnostics JSONL contains operational metadata but not request
text or repository contents.

Check readiness from another process with:

```bash
tgv health --status-file .tgv/status.json --max-age 15 --json
```

See [Production Operations](operations.md) for deployment and monitoring guidance.

## Read-only MCP mode

```bash
python -m pip install -e '.[mcp]'
tgv-mcp --repo .
```

`tgv-mcp` uses an explicit read-only service mode. MCP repository search and context preparation do
not persist `.tgv/index.json`, outcome records, or telemetry.

## Next steps

- [TGV and TER end-to-end workflow](tgv-ter-workflow.md) — canonical measured-evidence path.
- [TER integration contract](ter-integration.md) — pinned CLI and JSON assumptions.
- [Routing intelligence](routing-intelligence.md) — profiles, scoreboard, and outcome ledger.
- [Benchmarks and diagnostics](benchmarks.md) — observed benchmark and confidence definitions.
- [Architecture](architecture.md) — component boundaries and integration design.
- [Threat model](threat-model.md) — privacy and security limits.
