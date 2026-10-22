# TGV 0.32 release notes

TGV 0.32 moves the project from operational readiness toward a production-candidate posture. The release focuses on graceful lifecycle behavior, externally consumable observability, real outcome-based evaluation, and a more transparent routing UX.

## Highlights

- **Graceful draining and shutdown.** `tgv serve` now stops accepting new work after SIGTERM/SIGINT, marks readiness as draining, lets the in-flight request finish within `--shutdown-grace-seconds`, and terminates the isolated worker if the grace period expires.
- **Prometheus-compatible metrics export.** `--metrics-file` writes atomic Prometheus textfile metrics for readiness, draining state, request outcomes, route counts, worker restarts, routing latency, and provider calls/errors. `tgv metrics --status-file ...` renders the same status on demand.
- **Real outcome-ledger evaluation.** `tgv benchmark outcomes` evaluates observed privacy-minimal outcomes rather than synthetic labels. It reports verified success, human acceptance, rework, retries, safety, latency, and estimated cost by model, capability tier, and task category. `--enforce` can require minimum verified evidence, minimum success, and a maximum safety-violation rate.
- **Counterfactual route explanations.** `tgv explain` shows why the selected route won and estimates what request/history/policy changes could make each alternative route win. This complements the raw machine evidence already exposed by `tgv route --explain`.
- **Private atomic local state.** Daemon status, Prometheus metrics, repository indexes, telemetry session files, and generated context-bundle files are written atomically and use owner-only permissions on POSIX-compatible filesystems. Outcome SQLite databases are also tightened to owner-only mode where supported.
- **Preserved routing quality gate.** The 104-case integrated benchmark remains CI-enforced and the original deterministic routing benchmark remains unchanged.

## Validation

The v0.32 source tree passes the full local pytest suite above the required 98% branch-aware coverage threshold. The integrated 104-case intelligence benchmark and 76-case deterministic routing benchmark remain release gates/diagnostics.

The environment used to prepare this archive does not have Ruff, mypy, or `pip-audit` installed, so those three checks are not claimed as locally executed. They remain configured in CI.
