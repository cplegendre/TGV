# Local routing and index diagnostics

TGV includes two deterministic, offline diagnostics. They are regression tools for a repository and fixture state, not universal performance claims.

For the end-to-end workflow that produces linked outcome and TER evidence, see
[TGV and TER end-to-end workflow](tgv-ter-workflow.md).

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

Linked real agent sessions can be associated with durable outcome tasks and persisted as
aggregate TER measurements. To collect that evidence, link each external agent session, record
outcome completion and validation evidence, and run:

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

For each linked session, TGV uses the deterministic canonical measurement policy: the
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

Malformed persisted records never abort the report or echo raw values. A `metadata` value that is
not a JSON object is treated as `{}` and counted as `invalid_metadata_tasks`; an invalid confidence
value is reported as null and counted. A row that cannot be decoded is quarantined together with
its dependent attempts, sessions, and measurements, excluded from every denominator, and never
counted as a verified success or failure. These counts appear under
`coverage.invalid_records` (also `overall.invalid_records` in `report --linked`). Read-only reporting
never creates or migrates the ledger; SQLite may create transient WAL coordination sidecars for an
existing WAL database. See
[invalid and unreadable records](tgv-ter-workflow.md#invalid-and-unreadable-records).

The benchmark is observational evidence, not a causal experiment. Selection
bias, task difficulty, model-selection confounding, missing measurements, unequal verification
coverage, analyzer-version differences, repeated attempts, and the absence of counterfactual
executions can all affect comparisons. Do not use this report to claim that another routing
strategy would have performed better on the same tasks.

## Confidence diagnostics

```bash
tgv confidence --repo .
tgv confidence --repo . --json
# Optional configuration, including intelligence.outcome_database_path
tgv confidence --repo . --config tgv.yaml --json
```

This command measures observed alignment between persisted routing confidence and verified
outcomes. It does not execute agents, invoke TER, load session content, create indexes, or
modify routing/configuration. It uses the same read-only ledger access and schema checks as
the observational benchmark: missing ledgers produce empty reports, incompatible legacy
schemas require an explicit upgrade elsewhere, and task records that cannot be decoded produce a
controlled error without echoing task content. A `metadata` value that is not a JSON object is
treated as unavailable provenance instead of aborting the report. SQLite may create transient WAL
coordination sidecars for a live WAL database; the command does not change journal mode or
permissions. Configured relative
database paths resolve against `--repo`; absolute paths are used as configured.
See [the confidence workflow step](tgv-ter-workflow.md#13-run-confidence-diagnostics) for its
place in the full evidence pipeline.

### Observation and outcome definitions

One unique outcome task contributes at most once, regardless of attempts or linked sessions.
Only verified outcomes with finite numeric confidence in [0, 1] are scored. Success is 1 and
failure is 0, using the existing outcome contract: completed/succeeded tasks need validation,
human acceptance, or rework evidence; observed negative evidence makes them failures.
Safety violations count as verified failures even if incomplete. Completion without evidence
is unverified; other incomplete tasks are excluded from scores. TER is never a prerequisite,
so verified failures without measurements remain included.

Missing, nonnumeric, nonfinite, out-of-range, and boolean confidence values are rejected
without clamping or fabrication. Historical SQLite REAL affinity can irreversibly turn
booleans into numeric 0/1, numeric strings into numbers, and NaN into NULL. Diagnostics validate
the persisted representation; they cannot recover types already lost by SQLite. Missing and
invalid confidence remain visible in coverage counts.

### Scores and uncertainty

For the `n` scored tasks, with confidence `p_i` and verified outcome `y_i`:

- **Brier score:** `sum((p_i - y_i)^2) / n`.
- **Reliability bins:** ten fixed intervals `[0.0, 0.1)`, ..., `[0.9, 1.0]`.
  Internal boundaries enter the higher bin; 0 enters the first and 1 the last. Each bin
  reports its count, mean confidence, verified-success frequency, absolute gap, and Wilson
  95% interval for the success frequency.
- **Expected calibration error (ECE):** the sum of each bin's absolute gap weighted by
  `bin_count / n`. Group scores use only that group's observations.

Calculations retain floating-point precision; output is rounded to six decimal places.
Empty bins have count zero and null statistics. Brier and ECE are null when `n = 0`.
Wilson intervals use the existing full-precision Wilson score formula with
`z = 1.959963984540054`. They describe uncertainty in observed success frequencies, not
uncertainty in Brier/ECE, and do not correct selection bias or dependence between tasks.

### Coverage, groups, and sufficiency

Every overall/group report includes total tasks, verified successes/failures, completed but
unverified tasks, incomplete tasks, valid/missing/invalid confidence, and scored observations.
Coverage denominators are explicit:

| Field | Numerator / denominator |
|---|---|
| `verification_coverage` | verified tasks / all tasks |
| `confidence_coverage` | tasks with valid confidence / all tasks |
| `scoring_coverage` | scored observations / all tasks |
| `verified_success_rate` | verified successes / all verified tasks, including those without valid confidence |

Undefined ratios are null. Exclusion reasons can overlap; `excluded_tasks` counts unique
excluded tasks. The overall success interval also uses all verified tasks, while bin
intervals use only their scored tasks. Evidence is `missing` at zero scored observations,
`limited` at 1–19, and `sufficient` at 20 or more **for descriptive sample-size reporting
only**. Scores remain available for small samples; this convention is neither a quality gate
nor proof of statistical validity.

JSON provides full group-local metrics and bins by authoritative route, selected model,
capability tier, decision source, task category, and provenance status. Text shows overall
bins and compact group scores/counts; use JSON for full group coverage and intervals.
Missing route/source fields use `unavailable`; malformed values use `invalid-route` or
`invalid-decision-source`. These tasks remain scored when otherwise eligible and are also
separated by `missing-provenance` or `invalid-provenance`, alongside `attributable` records.
No route is inferred from a tier. Output is deterministic for identical persisted evidence,
with sorted groups, fixed bins, no generated identifiers or timestamps, and no individual
task records, prompts, responses, descriptions, session paths, transcripts, or TER spans.

### Interpretation limits

Routing confidence is heuristic or provider-reported; probability-style scoring is an
exploratory assumption. Overrides can retain baseline confidence after changing the selected
route, and pooled scores mix these meanings. See [routing confidence semantics](model-routing.md#routing-confidence-semantics).
Verification coverage, missing confidence, task difficulty, model selection, policy changes,
and repeated work under distinct task identifiers can bias results. Different verification
practices also limit comparisons between groups. Low ECE depends on binning and sample size
and does not establish optimal routing. Neither these scores nor minimum sample counts
authorize threshold adjustments, causal group comparisons, or claims of savings.
