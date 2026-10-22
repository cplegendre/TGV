# Getting started

1. Install TGV.
2. Run `tgv --demo`.
3. Run `tgv doctor`.
4. Route a real request.
5. Prepare a bundle.
6. Enable telemetry.
7. Run `tgv report`.
8. Start `tgv serve`.

The demo is fully local and synthetic; it requires no API key, Ollama, network access, TER, or other optional dependency. Its default 450 tasks are shown through a compact terminal progress bar, followed by aggregate PASS/FAIL, timing, throughput, and report metrics rather than one line per task. Use `tgv demo --scale medium`, `--scale large`, or `--scale stress` for 5,000, 25,000, or 100,000 deterministic routes; `--tasks N` overrides the preset with any positive count. Expanded runs cycle the canonical 450 tasks and are scalability demonstrations, not independent benchmark corpora.

# Getting started

TGV requires Python 3.11 or newer.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install tgv-agent
```

For a source checkout, use `python -m pip install -e .`.

## Check the environment

```bash
tgv doctor
```

The doctor report shows configuration status and availability for Ollama or an OpenAI-compatible endpoint configured for the `fast` role.

## Route a request

```bash
tgv route "Where is authentication configured?" --repo . --explain
```

High-risk security, destructive, concurrency, and architectural work always escalates to `frontier_deep`.

## Prepare a context bundle

```bash
tgv prepare "Fix refresh-token rotation" --repo . --output .tgv/bundles/auth --json
```

The bundle contains an `outcome_task_id` assumption. After review or handoff, record optional human feedback:

```bash
tgv outcomes complete TASK_ID --accepted --repo .
# or
tgv outcomes complete TASK_ID --rejected --repo .
```

Outcome-based scoreboard recommendations are **experimental**. Until the configured minimum sample count is met, TGV returns `fallback_used: true` with a reason and preserves the deterministic route.

## Enable telemetry

```yaml
telemetry:
  enabled: true
  log_path: .tgv/sessions
```

Telemetry is privacy-minimal but includes request text and routing metadata. Review the configuration before enabling it.

## Generate a value report

```bash
tgv report --sessions .tgv/sessions --json
```

The report keeps the stable machine-readable fields `local_share`, `savings_tokens`, and `savings_ratio` for compatibility. These fields are routing/token-allocation proxies, not a measured counterfactual of frontier usage. Human-readable demo output labels them as local-routed token volume/share proxies. By default
this works immediately with no extra install, using the coarse token estimates already
recorded in telemetry. For more precise aligned/waste-token scoring, install the optional
TER integration and add `--ter`:

```bash
pip install "tgv-agent[ter]"
tgv report --sessions .tgv/sessions --ter --json
```

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

Responses are written to stdout and structured request logs to stderr. Routing runs in an isolated persistent worker process. The daemon is marked ready only after that worker completes its startup handshake. If `--request-timeout` expires, the worker is terminated before the timeout is returned, on every supported platform; the next request starts a clean worker. The status file is updated atomically and includes heartbeat, request counters, route counts, worker restarts, and routing/provider metrics. Optional diagnostics JSONL contains operational metadata but not request text or repository contents.

Check readiness from another process with `tgv health --status-file .tgv/status.json --max-age 15 --json`. See `docs/operations.md` for deployment and monitoring guidance.


## Read-only MCP mode

`tgv-mcp --repo .` uses an explicit read-only service mode. MCP repository search and context preparation do not persist `.tgv/index.json`, outcome records, or telemetry.
