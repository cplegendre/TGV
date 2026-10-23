# Local routing and index diagnostics

TGV includes two deterministic, offline diagnostics. They are regression tools for a repository and fixture state, not universal performance claims.

## Routing benchmark

Run `tgv bench routing` or `python -m tgv.bench.routing`. Add `--json` for automation. The committed JSONL corpus contains 76 labeled cases spanning simple extraction, repository inspection, bounded code work, ambiguity, destructive/security/concurrency/architecture risk, privacy constraints, local-only requests, frontier handoff, and conflicting signals.

The report includes exact accuracy, acceptable-route accuracy, a confusion matrix, per-category accuracy, low-confidence correct decisions, high-confidence wrong decisions, and high-risk downgrade violations. CI requires at least 90% acceptable accuracy and zero high-risk violations.


## Integrated routing-intelligence benchmark

Run `tgv benchmark run --enforce --json` to evaluate task categorization, complexity bands, deterministic route acceptability, and false-low-risk behavior together. TGV 0.32 commits 104 curated synthetic cases covering all 13 task categories, with 32 low-, 56 medium-, and 16 high-complexity cases.

The release gate requires at least 100 cases, 90% task-category accuracy, 90% complexity-band accuracy, 95% acceptable-route accuracy, and a 0% false-low-risk rate. CI enforces the gate. These cases are intentionally broader than the 76-case low-level deterministic routing corpus, but remain a regression corpus rather than a claim about all real-world repositories or tasks.

## Index diagnostic

Run `tgv bench index --repo . --query "RouteDecision Repository"` or `python -m tgv.bench.indexing . --json`. The report includes eligible files indexed, skipped files, Python symbols extracted, build latency, search latency, and cache hits/misses.

Numbers depend on filesystem, repository size, cache state, and query. Compare repeated runs on the same fixture or repository rather than treating a small local result as a general benchmark.

## Observed outcome benchmark

Run `tgv benchmark outcomes --repo . --json` after a deployment has recorded outcomes. Unlike the curated release corpus, this diagnostic reads the repository-scoped privacy-minimal SQLite ledger and summarizes verified success, human acceptance, rework, retries, safety observations, latency, and estimated cost by model/tier/category.

`--enforce` defaults to at least 20 verified observations, at least 85% verified success, and zero observed safety violations. An undersampled ledger fails an enforced gate as insufficient evidence. Do not compare these rates across repositories without accounting for task mix and validation coverage.

## Observational routing benchmark

PR-05 linked real agent sessions to durable outcome tasks and persisted aggregate TER
measurements. To collect that evidence, link each external agent session, record outcome
validation evidence, and run:

```bash
tgv efficiency --linked --repo .
```

Real TER scoring requires a working TER 3.0.0 installation and its embedding backend.
After measurements have been persisted, run the observational benchmark without invoking TER:

```bash
tgv benchmark observed --repo . --json
```

The command opens the privacy-minimal outcome ledger in SQLite read-only mode. A missing
ledger produces an empty report without creating a database, and legacy schema versions are
reported as incompatible rather than migrated. SQLite may still create transient WAL
coordination sidecars when reading a live WAL database; TGV does not change the journal mode.
The report groups task-level routing and
outcome evidence with session-level TER measurements by route, selected model, capability
tier, and task category. Coverage uses linked sessions as the measurement denominator and
eligible tasks as the outcome denominator. A task counts once in verified-success rates even
when it has multiple attempts or linked sessions; attempt statuses and sessions remain
separately traceable as attempt/session-level diagnostics.

Route groups use the authoritative route persisted when `tgv prepare` records an outcome.
The capability tier is not used to reconstruct a route because an `ADVANCED` task can be
served either by deterministic `frontier_standard` routing or scoreboard-selected
`local_code` routing. Historical tasks created before route persistence are grouped under
`unavailable` rather than assigned a fabricated route.

For each linked session, TGV uses PR-05's deterministic canonical measurement policy: the
most recent valid measurement wins, with measurement id breaking ties. If multiple analyzer
versions are present, the token-weighted aggregate uses only the analyzer version with the
most measured sessions; ties break lexicographically. That one global scope is applied to
every route, model, tier, and category group, so groups without coverage in the selected
scope report `aggregate_ter: null`. Every analyzer scope remains visible so incompatible
versions are never silently combined.

Verified success means a completed task with positive validation evidence. A completed task
without verification evidence is unverified, and an incomplete task is reported separately.
TER efficiency is not correctness: a high TER can describe an efficient execution that still
failed verification, and a verified success can have low TER.

The report includes Wilson 95% confidence intervals for verified-success proportions and
explicit missing-data counts. Evidence sufficiency is reported independently of methodology:
it is missing when comparable measurements in the selected analyzer scope or verified
outcomes are absent, limited when either sample is below 20, and sufficient when both are
at least 20. This threshold is a conservative reporting warning, not a calibration gate or
proof of causality.

The benchmark is observational evidence, not a causal experiment. Selection
bias, task difficulty, model-selection confounding, missing measurements, unequal verification
coverage, analyzer-version differences, repeated attempts, and the absence of counterfactual
executions can all affect comparisons. Do not use this report to claim that another routing
strategy would have performed better on the same tasks.
