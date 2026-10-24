# PR-10 through PR-19 implementation review

Status is based on the v0.39 source tree, not on phase claims.

| PR | Area | Status | Evidence and remaining work |
|---|---|---|---|
| PR-10 | Measurement integrity | Complete | Hollow Claude sessions are rejected; linked zero-token TER results are rejected; duplicate links are blocked; batch measurements are atomic. Final hardening moved zero-token, negative-token, inconsistent-total, non-finite, and out-of-range TER validation into the persistence boundary so alternate ledger callers cannot bypass it. Historical rows remain readable and unmodified. |
| PR-11 | Attempt lifecycle | Complete | `outcomes complete` now validates task/repository/attempt correlation and commits task status, human feedback, and all pending-attempt updates in one `BEGIN IMMEDIATE` transaction. The ledger enforces `pending -> running/completed/failed`, `running -> completed/failed`, and idempotent terminal updates. Retry workflows preserve failed attempts; unknown historical statuses are not rewritten. |
| PR-12 | Reporting integrity | Complete | Read-only evidence reads, missing-ledger handling, quarantined records, and malformed metadata handling are present. `outcomes list`/`export` do not create a missing ledger. |
| PR-13 | TER-side Codex loader | Missing / TER-dependent | No Codex loader exists in the local TER checkout. TGV cannot provide or certify it without modifying TER. |
| PR-14 | Codex session validation and linking | Complete (link-only) | `--agent codex` validates `rollout-*.jsonl`, `session_meta` identity, symlink/regular-file/oversize constraints, and meaningful user/assistant text. Linked Codex scoring fails closed with an actionable error. |
| PR-15 | Versioned TER contract | Partial | TGV parses `schema_version: 1` and legacy unversioned TER 3.0.0 output, rejects unsupported versions/schemas, and validates identity/totals. The pinned TER 3.0.0 producer does not emit `schema_version`, so end-to-end versioned compatibility is not verifiable. |
| PR-16 | Explicit session replacement | Complete | Replacement requires `--replace-unmeasured --attempt`, preserves link identity, checks task/repository/agent/session identity, refuses measured links, and updates hash plus locator in one `BEGIN IMMEDIATE` transaction. Tests cover identity, locator conflicts, wrong attempts, and attempt-scoped measurements. |
| PR-17 | Documentation and release consolidation | Complete | Version references, TER compatibility, Codex link-versus-score, read-only commands, zero-token semantics, lifecycle behavior, and release checklists are corrected for this candidate. |
| PR-18 | Statistical improvements | Partial | Deterministic opt-in bootstrap intervals, Brier score, ECE, and reliability bins are implemented and tested. Attempt-scoped outcome pairing and decision-source eligibility diagnostics remain outstanding. |
| PR-19 | Legacy calibration safety | Complete | `calibrate --apply` without `--evidence` requires `--accept-legacy-heuristic`; the flag is rejected in evidence mode. Read-only legacy proposals remain available, and evidence calibration stays non-applying. |

## Release-critical versus optional work

Release-critical functionality is complete in TGV:

- Atomic outcome persistence and rollback.
- Valid execution-attempt lifecycle transitions.
- Persistence-level TER measurement integrity.
- Local test, coverage, lint, format, typing, packaging, version, and benchmark gates.

Optional or TER-dependent follow-up remains:

- TER-side Codex loader.
- TER-side `schema_version` producer.
- Attempt-scoped calibration pairing.
- Remaining PR-18 grouped diagnostics and decision-source eligibility counts.
- CodeQL activation.

## TER-dependent boundaries

- TGV supports TER 3.0.0 unversioned output and synthetic/tested
  `schema_version: 1` output.
- TER 3.x without `schema_version` is rejected unless the detected analyzer is
  exactly 3.0.0.
- TER 4 and later are rejected by major version.
- Claude Code is the only TER 3.0.0 scoring format.
- Codex can be linked and validated by TGV but cannot be scored by the bundled
  TER analyzer.
- TER-side producer and loader changes are documented in `docs/CODEX_AUDIT.md`
  and were not applied.

## Test coverage added or updated

- Atomic completion success, multi-attempt completion, rollback under injected
  execution/feedback/attempt failures, repeated completion, missing/corrupt
  ledgers, unknown tasks, and repository/correlation rejection.
- Valid, idempotent, invalid, terminal, retry, and unknown-historical attempt
  status transitions.
- Valid measurement persistence, generic metadata, zero/negative/boolean/float
  tokens, inconsistent totals, non-finite and out-of-range TER, missing TER
  fields, batch rollback, and historical-row compatibility.
- Zero-token legacy aggregation and threshold-proposal safety.
- Null-safe text rendering for linked and legacy reports.
- Read-only `outcomes list`/`export` missing-ledger behavior.
- Codex loaders: response-item messages, malformed envelopes, missing identity,
  conflicting identity, filenames, malformed JSON, and symlinks.
- Codex scoring fail-closed behavior without a TER loader.
- Session replacement: identity, locator conflict, wrong task, measured-attempt
  immutability, and high-level replacement authorization.
- Benchmark version consistency.
