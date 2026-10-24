# Production Operations

TGV 0.39's stdio daemon is intended to run behind a local process supervisor or an integration that owns stdin/stdout. It is a production-candidate local component, not an authenticated public network service or sandbox.

## Recommended invocation

```bash
tgv serve --repo /srv/project \
  --config /etc/tgv/tgv.yaml \
  --status-file /run/tgv/status.json \
  --diagnostics-file /var/log/tgv/diagnostics.jsonl \
  --metrics-file /var/lib/node_exporter/textfile_collector/tgv.prom \
  --request-timeout 120 \
  --heartbeat-interval 5 \
  --shutdown-grace-seconds 10
```

Keep stdout reserved for the JSONL request/response protocol. Send stderr and the optional diagnostics file to the host logging system.

## Lifecycle and graceful drain

The daemon owns one persistent routing worker process created with multiprocessing `spawn`. Readiness becomes true only after the worker startup handshake succeeds.

On SIGTERM or SIGINT:

1. readiness becomes false and state becomes `draining`;
2. no new input request is accepted;
3. the current request may finish for up to `--shutdown-grace-seconds`;
4. if it is still running, the worker process is terminated;
5. final state is written as `stopped`.

Input reading happens on a bounded daemon thread so a blocking stdin read does not prevent shutdown from progressing. Request deadlines remain independent of the shutdown grace period.

## Health and readiness

```bash
tgv health --status-file /run/tgv/status.json --max-age 15 --json
```

A healthy result requires a fresh heartbeat and `ready=true`. Draining is reported distinctly from a generic unavailable state. The status document includes lifecycle state, worker PID/activity/restarts, request success/failure/timeout/cancellation counts, route counts, last error, and bounded service/provider metrics.

## Prometheus textfile metrics

`--metrics-file` atomically writes Prometheus-compatible text exposition suitable for the node_exporter textfile collector or another file-based scraper. You can also render metrics from a status file:

```bash
tgv metrics --status-file /run/tgv/status.json --format prometheus
```

Current metrics include:

- `tgv_up`, `tgv_ready`, and `tgv_draining`;
- request totals by handled/succeeded/failed/timed-out/cancelled result;
- route request totals;
- worker restart count;
- routing total/errors and bounded average/p50/p95/max latency;
- local-provider calls and errors by provider/model.

Metrics and diagnostics deliberately exclude request text and repository contents. Provider/model labels may still be operationally sensitive.

## Real deployment evidence

The curated 104-case corpus is a regression gate, not production evidence. Once a deployment has outcome records, evaluate them separately:

```bash
tgv benchmark outcomes --repo /srv/project --json

tgv benchmark outcomes --repo /srv/project \
  --minimum-verified-samples 20 \
  --minimum-success-rate 0.85 \
  --maximum-safety-violation-rate 0 \
  --enforce
```

The outcome benchmark is repository-scoped and reports verified success, acceptance, rework, retry, safety, latency, and estimated cost by model, capability tier, and task category. Insufficient verified evidence fails an enforced gate rather than being treated as success.

## Local state permissions

On POSIX-compatible filesystems, TGV 0.39 uses owner-only permissions for atomic status/metrics files, repository indexes, telemetry session files, generated context bundles, and the SQLite outcome database where supported. These defaults reduce accidental disclosure but do not replace OS account separation, directory ACLs, encrypted storage, or backup policy.

Run TGV under a dedicated service account when multiple users share a host. Prefer environment variables for provider secrets and keep command-line credentials out of process listings and shell history.

## Diagnostics and retention

`--diagnostics-file` records one JSON object per request with request ID, selected route, latency, error type, timeout/cancellation state, and worker restart count. It does not contain the request body or repository content.

Rotate diagnostics with the host's normal log-rotation mechanism. TGV does not currently implement retention/rotation itself.

## Recommended supervisor policy

- Restart the daemon on unexpected process exit.
- Treat `tgv health` failure as not-ready and stop sending new work.
- Give the supervisor's termination timeout more time than `--shutdown-grace-seconds` so TGV can finish its drain path.
- Set OS memory/CPU/process limits appropriate to the repository and local model workload.
- Keep stdout reserved for JSONL protocol traffic.
- Alert on repeated worker restarts, cancellations/timeouts, provider error-rate increases, or sustained p95 latency growth.
- Scrape or collect the Prometheus textfile on a cadence shorter than the heartbeat freshness window.
- Back up the outcome database only if adaptive history is operationally valuable; it is not required to reconstruct the application.

## Pre-production checklist

```bash
python -m pytest
tgv bench routing --json
tgv benchmark run --enforce --json
tgv doctor --config /etc/tgv/tgv.yaml
tgv explain "Representative production task" --repo /srv/project
```

After representative outcome data exists, add `tgv benchmark outcomes --enforce`. In a network-enabled build environment also run the configured Ruff, strict mypy, package build, and `pip-audit` CI gates. Review `SECURITY.md` and `docs/threat-model.md`, especially deny patterns and external-agent command configuration.

## Current limitations

- stdio only; there is no authenticated network routing transport;
- one routing request is processed at a time;
- Prometheus export is file-based rather than an HTTP/OpenTelemetry endpoint;
- diagnostic rotation is delegated to the host;
- the 104-case release corpus is curated/synthetic, while real-world quality depends on deployment-specific outcome collection;
- process termination guarantees local worker cancellation but cannot revoke work already accepted by an external remote provider.
