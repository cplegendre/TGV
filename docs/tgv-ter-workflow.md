# TGV and TER end-to-end workflow

This is the canonical guide for using TGV routing evidence with
[TER](https://github.com/cplegendre/TER) execution measurements. It applies to TGV 0.39 and
the pinned TER CLI 3.0.0 contract. TER is optional: routing, context preparation, outcome
persistence, and confidence diagnostics work without it.

The workflow is deliberately observational. It links a real agent session to the TGV task that
preceded it, records verification evidence, measures the session with TER, and reports the
resulting evidence. It does not prove that one route or model would have performed better under
a counterfactual policy.

## Responsibilities at a glance

| TGV | TER |
|---|---|
| Task classification and profiling | Agent-session parsing |
| Deterministic routing and safety policy | Token accounting |
| Repository evidence and context preparation | Span classification |
| Execution handoff and outcome persistence | Execution-efficiency analysis |
| Verified-success evaluation | TER scoring and aggregate metrics |
| Routing benchmarks, evidence calibration, and confidence diagnostics | No routing or outcome decisions |

TGV calls TER through its public CLI. It does not import TER internals, share TER's database, or
reimplement TER's scoring algorithms. TER does not route tasks or decide whether a task passed.

## Prerequisites

- Python 3.11 or newer.
- A repository that TGV may read.
- A supported external agent session in Claude Code JSONL format for linked measurement.
- Optional: network access on the first TER analysis so its embedding model can be downloaded,
  or a pre-cached model for offline operation.

The commands below use these placeholders:

- `TGV_REPO`: absolute path to the repository being routed.
- `TASK_ID`: UUID assigned by `tgv prepare`; it is recorded in the bundle assumptions.
- `SESSION_JSONL`: path to the real Claude Code session file.
- `TGV_CONFIG`: optional TGV configuration file, when not using defaults.

## 1. Install TGV

Install the published package in an isolated environment:

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

Verify the installation and the deterministic local demo:

```bash
tgv --version
tgv --demo
```

The demo is synthetic and does not require TER, a provider, or network access. It is an
installation check, not production evidence.

## 2. Install and configure TER

Install TGV's pinned TER extra:

```bash
python -m pip install 'tgv-agent[ter]'
```

From a TGV source checkout, use:

```bash
python -m pip install -e '.[ter]'
```

The extra installs `ter-calculator[embeddings]` from TER Git tag `v3.0.0`, including the
embedding backend. TER adds substantial optional dependencies; keep it out of minimal
deployments that only need routing.

Verify the executable on `PATH`:

```bash
ter --version
```

TGV accepts unversioned JSON only from TER `3.0.0`. Newer TER 3.x builds are accepted only
when their JSON declares `schema_version: 1`. TER versions below `3.0.0`, TER 4+, unsupported
schema versions, unparseable versions, invalid JSON, and analysis failures are reported as TGV
errors.
The first analysis may download an embedding model. For offline use, pre-cache the model
according to TER's documentation and do not assume TGV performs that download for you.

Optionally confirm the public analysis contract on the same session file before linking it:

```bash
ter analyze /path/to/agent-session.jsonl --format json
```

## 3. Create and route a task

Inspect the routing decision first. This command does not create an outcome task:

```bash
tgv route "Fix refresh-token rotation without changing the public API" \
  --repo "$TGV_REPO" --json
```

TGV returns a profile (`local_fast`, `local_code`, `frontier_standard`, or `frontier_deep`),
confidence, model selection, decision source, rationale, and risk signals. High-risk security,
destructive, concurrency, and architectural work can override lower tiers. Routing is
deterministic unless configured model suggestions pass their confidence thresholds.

`RouteDecision.confidence` is confidence in the routing choice. It is not a calibrated
probability that the task will pass verification.

## 4. Prepare the execution context

Create the durable task and its evidence-backed handoff:

```bash
tgv prepare "Fix refresh-token rotation without changing the public API" \
  --repo "$TGV_REPO" \
  --output "$TGV_REPO/.tgv/bundles/refresh-token" \
  --mode handoff-only \
  --json
```

Add `--config "$TGV_CONFIG"` only when using a configuration file. `--mode handoff-only` writes
`.tgv/bundles/refresh-token/bundle.json` and `.tgv/bundles/refresh-token/handoff.md` without
launching an external process. The bundle's assumptions include the outcome task identifier.
Extract it into the shell variable used by later examples:

```bash
TASK_ID=$(python -c 'import json, sys; bundle = json.load(open(sys.argv[1], encoding="utf-8")); print(next(item.split("=", 1)[1] for item in bundle["assumptions"] if item.startswith("outcome_task_id=")))' \
  "$TGV_REPO/.tgv/bundles/refresh-token/bundle.json")
printf '%s\n' "$TASK_ID"
```

The `tgv prepare --json` response reports the output directory, command, and dry-run state; it
does not echo the task identifier. Read it from `bundle.json` as shown above.

## 5. Execute with an external coding agent

Give the external agent `handoff.md` and, when useful, `bundle.json`. Then let the agent execute
the task in the repository using its normal tools and approval policy. TGV prepares and hands off
context; it does not automatically run, observe, or capture an external agent session.

If command handoff is configured, `--mode command` can invoke a configured argument array. That
still does not discover or link the resulting agent transcript. Linking is always an explicit
step.

## 6. Link the real session to the outcome task

Claude Code JSONL sessions can be linked as follows:

```bash
tgv outcomes link "$TASK_ID" \
  --session "$SESSION_JSONL" \
  --agent claude-code \
  --repo "$TGV_REPO"
```

The session must contain a consistent `sessionId`, at least one `user` record, and at least one
`assistant` record. TGV rejects missing files, directories, final-component symlinks,
non-regular files, oversized files, malformed records, repository mismatches, and conflicting
or changed links. Codex JSONL rollout linking is available with `--agent codex`; this
validates the format and identity, but **does not enable TER scoring for Codex**. The pinned
TER 3.0.0 analyzer has no verified Codex loader. Never interpret a Codex link as a valid
TER efficiency measurement.

Linking validates and fingerprints the file but does not invoke TER. TGV does not scan home
directories or discover sessions automatically.

## 7. Record completion and verification evidence

Completion and verification are separate. Record the human outcome:

```bash
tgv outcomes complete "$TASK_ID" --accepted --repo "$TGV_REPO"

# For a rejected or failed task:
# tgv outcomes complete "$TASK_ID" --rejected --repo "$TGV_REPO"
```

When tests and static checks actually ran, record their user-attested results:

```bash
tgv outcomes validate "$TASK_ID" --tests-passed --static-checks-passed --repo "$TGV_REPO"

# Record observed negative evidence when applicable:
# tgv outcomes validate "$TASK_ID" --tests-failed --repo "$TGV_REPO"
```

TGV does not execute those tests or static checks. Under the existing verified-success contract,
a task is verified only when it is completed/succeeded and has at least one of test, static-check,
human-acceptance, or rework evidence. Negative test or static evidence makes it a verified failure;
human rejection or required rework also makes it a failure. A completed task without evidence
remains **unverified**, and an incomplete task is excluded from verified-success rates. Linking a
session alone never makes a task successful.

Task outcome status and execution-attempt status are separate fields. New completions close
associated pending attempts; historical ledger entries can still retain legacy pending states.
Review older attempts separately rather than assuming a pending count represents active work.

## 8. Analyze execution efficiency with TER

Analyze every linked session and persist the aggregate TER measurement:

```bash
tgv efficiency --linked --repo "$TGV_REPO" --format json
```

Add `--config "$TGV_CONFIG"` when using a configuration file. Linked mode revalidates every session
file and its SHA-256 fingerprint, writes the validated bytes to an owner-only temporary snapshot,
checks TER's version, requires TER's returned `session_id` to match the linked session, and commits
all measurements in one SQLite transaction. The temporary snapshot is removed after success or
failure. New measurement writes also reject zero-token evidence, negative or non-integral token
counts, inconsistent totals, non-finite TER, and TER outside `[0, 1]` at the ledger boundary, so a
caller cannot bypass measurement integrity. Historical rows remain readable and are not rewritten.

Use `--linked` for real agent sessions. `tgv efficiency --sessions PATH` scores the JSONL files
in `PATH`; pointing it at TGV's telemetry measures synthetic routing narration, not agent
execution. The two modes cannot be combined.

## 9. Store and inspect linked measurements

Inspect persisted measurements without invoking TER:

```bash
tgv report --linked --repo "$TGV_REPO" --json
```

Unlike `tgv efficiency --linked`, this command is strictly read-only: it never invokes TER,
creates the database, migrates the schema, or writes measurements. When the ledger does not exist,
the report returns `status: "empty"` with zero counts instead of creating
`.tgv/outcomes.sqlite3`. SQLite may create transient WAL coordination sidecars for an existing WAL
database; the ledger contents and schema are unchanged.

The linked report separates raw measurements from deterministic aggregates. For each linked
session, the most recent valid measurement wins, with measurement ID breaking ties. When multiple
TER analyzer versions exist, one global analyzer/version scope with the most measured sessions is
used for comparisons; ties break lexicographically. Earlier measurements remain visible as
superseded records but do not count twice.

### Invalid and unreadable records

The read-only evidence commands (`report --linked`, `outcomes list`, `outcomes export`,
`benchmark observed`, `calibrate --evidence`, and `confidence`) never create or migrate the
ledger and never emit a traceback or raw persisted values for malformed data:

- A `metadata` value that is not a JSON object is treated as `{}`, counted as
  `invalid_metadata_tasks`, and listed under `invalid_records`. Provenance derived from it becomes
  `unavailable` or invalid (`invalid-route` / `invalid-decision-source` in confidence diagnostics);
  the task itself remains usable evidence.
- An invalid `routing_confidence` value is reported as null and counted. In confidence
  diagnostics it remains visible in coverage but is excluded from Brier/ECE scoring, exactly as in
  earlier releases.
- A row that cannot be decoded (missing required field, unexpected type, unreadable enum, invalid
  JSON in a list field) is quarantined together with its dependent attempts, sessions, and
  measurements. Quarantined records are excluded from every denominator, are never counted as
  verified successes or failures, and are counted under `invalid_records`.
- An unsupported schema version aborts the report with a controlled error; read-only commands never
  migrate the schema. Upgrade the ledger with a writable TGV command first.

`invalid_records` appears under `overall` in the linked and observational reports and under
`coverage` in evidence calibration. Every block states the exclusion policy, so a reduced task
count can be distinguished from missing data.

## 10. Generate outcome and efficiency reports

Outcome-only evidence does not require TER:

```bash
tgv benchmark outcomes --repo "$TGV_REPO" --json
```

Linked efficiency evidence, including task verification and measured sessions, is reported by:

```bash
tgv report --linked --repo "$TGV_REPO" --json
tgv outcomes export --repo "$TGV_REPO" --format csv --limit 100
```

The outcome benchmark summarizes verified success, human acceptance, rework, retries, safety
observations, latency, and estimated cost by model, tier, and category. It is repository-scoped.
Do not compare rates across repositories without accounting for task mix and verification
coverage.

## 11. Run the observational benchmark

After linked measurements and verified outcomes exist, combine them without invoking TER:

```bash
tgv benchmark observed --repo "$TGV_REPO" --json
```

This report groups task-level routing and outcome evidence with session-level TER measurements by
route, selected model, capability tier, and task category. Each task counts once in verified
success rates even when it has multiple attempts or sessions. The report describes observed
associations only; there is no counterfactual execution.

Evidence sufficiency is `missing` when comparable measurements or verified outcomes are absent,
`limited` below 20 of either sample, and `sufficient` at 20 or more of each. These are descriptive
reporting warnings, not an intervention gate or proof of external validity.

## 12. Run evidence-gated calibration

Inspect the conservative calibration diagnostic:

```bash
tgv calibrate --evidence --repo "$TGV_REPO" --json
```

Add `--config "$TGV_CONFIG"` when using a configuration file. Evidence mode reads the ledger without
creating, migrating, or repairing it. It targets only authoritative `local_fast` and `local_code`
tasks whose decision source is `model`; deterministic, scoreboard, safety-override, frontier,
missing, and invalid provenance are excluded rather than inferred.

Current recommendations preserve every configured threshold, report `direction=none`, and set
`application_permitted=false`, even when the correctness diagnostic passes. The descriptive
availability minimums are 20 verified outcomes, 20 comparable measurements, and 20 paired tasks per
target. The correctness diagnostic also requires an unrounded 95% Wilson lower bound of at least
0.90. These are observational checks, not proof that changing a threshold would improve outcomes.

`--apply` remains an explicit compatibility interface, but the current evidence engine emits no
eligible proposal and writes no configuration. Do not expect it to tune routing.

## 13. Run confidence diagnostics

Compare persisted routing confidence with verified outcomes:

```bash
tgv confidence --repo "$TGV_REPO" --json
```

Use the text form for a compact overview:

```bash
tgv confidence --repo "$TGV_REPO"
```

The command is read-only: it does not execute agents, invoke TER, load session contents, create
the ledger, migrate schemas, or change routing. TER measurements are never required. It scores one
unique task at most once, using only verified outcomes with finite numeric confidence in `[0, 1]`.
Completed-unverified and incomplete tasks are excluded from scores and remain visible in coverage
counts. Missing, nonnumeric, nonfinite, out-of-range, and boolean confidence values are rejected
without clamping or fabrication.

Malformed `metadata` is not fatal: a value that is not a JSON object is treated as unavailable
provenance (`invalid-route` / `invalid-decision-source`) instead of aborting the report. Task rows
that cannot be decoded produce a controlled error without echoing persisted values, and unsupported
schema versions are never migrated.

The report includes:

- **Brier score:** `sum((confidence - outcome)^2) / scored_observations`; lower is better.
- **Expected calibration error (ECE):** the weighted absolute gap between confidence and observed
  success across ten fixed reliability bins; lower is better, but binning and sample size matter.
- **Reliability bins:** `[0.0, 0.1)` through `[0.9, 1.0]`, with internal boundaries entering the
  higher bin. Each bin reports count, mean confidence, verified-success rate, gap, and a Wilson
  95% interval for the success rate.
- **Coverage:** verified, unverified, incomplete, valid/missing/invalid-confidence, and scored-task
  counts with explicit denominators.
- **Evidence sufficiency:** `missing` at zero scored tasks, `limited` at 1–19, and `sufficient` at
  20 or more. The threshold is descriptive only; small-sample scores remain available.
- **Group-local results:** authoritative route, selected model, capability tier, decision source,
  task category, and provenance status. No route is inferred from a tier.

Output is deterministic for unchanged persisted evidence: groups are sorted, bins are fixed, and
there are no generated identifiers or timestamps. See
[confidence diagnostics](benchmarks.md#confidence-diagnostics) for formulas and field definitions.

## 14. Interpret the evidence

Use the reports together, and keep these distinctions explicit:

| Distinction | Meaning |
|---|---|
| Synthetic routing telemetry vs real session | TGV telemetry records request text and a synthetic routing reply. TER measurement of it is not agent efficiency. Linked Claude Code sessions are real execution evidence. |
| Estimated vs measured tokens | TGV telemetry token counts are coarse estimates. TER aligned, waste, and total tokens are analyzer measurements of the supplied session. |
| Completed vs verified success | Completion is a status. Verified success requires positive evidence under TGV's outcome contract; unverified completion is not success. |
| Routing confidence vs calibrated probability | Routing confidence is heuristic or provider-reported. Brier/ECE diagnostics test whether it behaves like a success probability; they do not redefine it as one. |
| Observational association vs causal improvement | These reports describe tasks that happened. They do not estimate what another route would have done to the same task. |
| Routing overhead vs agent efficiency | TGV routing work is not automatically part of the external agent transcript. TER scores the agent session it is given, not TGV's internal routing cost. |

Verification coverage, missing confidence or measurements, task difficulty, model selection,
policy changes, repeated work under distinct task identifiers, and selection bias can all affect
the results. Wilson intervals describe sampling uncertainty in observed rates; they do not remove
those biases. A low Brier score or ECE does not prove optimal routing, and a high TER does not prove
correctness. No diagnostic in this workflow authorizes routing-threshold or scoreboard-weight
changes.

## Persistence and privacy

The repository-scoped SQLite outcome ledger stores task identifiers, timestamps, a repository
identifier derived from the repository path, task category, complexity, routing confidence, final
route and decision source, selected model and capability tier, routing reasons, retrieved file and
symbol identifiers, token estimates, validation flags, execution status, retry/cost/latency fields,
and optional metadata. The raw request is not stored unless
`intelligence.retain_raw_prompts` is explicitly enabled; a SHA-256 request hash is stored either
way.

For a linked session, TGV stores the agent name, external session identifier, SHA-256 content
fingerprint, attempt correlation, timestamps, and an **absolute session file locator** in the
`session_locators` table. Raw prompts, responses, transcripts, classified spans, and session file
contents are not copied into the ledger. The original session file remains at its external path.
The locator is needed to revalidate and re-analyze that exact file later, so it is sensitive
metadata: absolute paths can reveal project names, usernames, or directory structure.

TER measurements persist the analyzer name, analyzer version, measurement timestamp, and aggregate
metrics (TER, aligned tokens, waste tokens, total tokens). TGV's linked analysis uses an owner-only
temporary snapshot (`0400` where supported) and removes it after the run. TER may process detailed
span text internally; TGV's adapter persists only aggregates.

On POSIX-compatible filesystems, TGV makes a best-effort `0600` permission change on the SQLite
ledger and uses WAL mode with a busy timeout. These controls do not replace OS account separation,
directory ACLs, encryption, or backup policy. The ledger has no built-in retention, rotation,
deletion, or vacuum policy; protect and manage `.tgv` according to your data-handling requirements.

## TER compatibility and supported interface

- **Package:** `ter-calculator[embeddings]` pinned to TER Git tag `v3.0.0`.
- **Executable:** `ter` must be on `PATH`. Unversioned JSON output is supported only from
  legacy TER `3.0.0`; versioned JSON output requires supported `schema_version: 1` and a
  compatible TER 3.x analyzer version.
- **Analysis command:** `ter analyze <session.jsonl> --format json`.
- **Required JSON fields:** `session_id`, `aggregate_ter`, `aligned_tokens`, and `waste_tokens`.
  `total_tokens` is optional; when supplied it must equal aligned plus waste tokens.
- **Tolerated data:** additional fields such as `classified_spans` are ignored by TGV's adapter.
- **Validation:** TER errors, invalid JSON, unsupported schema/version combinations, session identity mismatches,
  out-of-range values, and inconsistent totals are rejected with a TGV error.

TER manuscript tags `v4` and `v5` are not verified TER package releases for this integration.
Do not claim compatibility with a TER release unless the pinned contract and a real analysis have
been tested. Both `claude-code` and `codex` may be linked, but only Claude Code is supported by
the pinned TER 3.0.0 scoring workflow. TER-side Codex loader work remains outstanding.

## Remaining limitations

- Linking is manual; TGV does not capture external agent sessions.
- Claude Code is the only supported TER 3.0.0 scoring format; Codex linking alone is not scoring.
- A linked session is evidence for one attempt; it is not a controlled experiment.
- The outcome ledger is local and has no built-in retention policy.
- Confidence calibration and evidence-gated calibration are read-only diagnostics with descriptive
  minimum samples, not authorization for policy changes.
- Real TER analysis depends on the optional embedding stack and may require network access or a
  pre-cached model.
