# PR-10 through PR-19 implementation review

Status is based on the v0.39 source tree, not on phase claims.

| PR | Area | Status | Evidence and remaining work |
|---|---|---|---|
| PR-10 | Measurement integrity | Complete | Hollow Claude sessions are rejected; linked zero-token TER results are rejected before persistence; duplicate links are blocked; batch measurements are atomic. Legacy zero-token aggregation was corrected to null TER during this review. |
| PR-11 | Attempt lifecycle | Complete with defects | `complete_pending_attempts` is idempotent and preserves non-pending statuses. CLI `outcomes complete` still performs three separate commits, so a mid-sequence failure can leave partial state. |
| PR-12 | Reporting integrity | Complete | Read-only evidence reads, missing-ledger handling, quarantined records, and malformed metadata handling are present. `outcomes list`/`export` were fixed during this review to stop creating a missing ledger. |
| PR-13 | TER-side Codex loader | Missing / TER-dependent | No Codex loader exists in the local TER checkout. TGV cannot provide or certify it without modifying TER. |
| PR-14 | Codex session validation and linking | Complete (link-only) | `--agent codex` validates `rollout-*.jsonl`, `session_meta` identity, symlink/regular-file/oversize constraints, and meaningful user/assistant text. Linked Codex scoring fails closed with an actionable error. |
| PR-15 | Versioned TER contract | Partial | TGV parses `schema_version: 1` and legacy unversioned TER 3.0.0 output, rejects unsupported versions/schemas, and validates identity/totals. The pinned TER 3.0.0 producer does not emit `schema_version`, so end-to-end versioned compatibility is not verifiable. |
| PR-16 | Explicit session replacement | Complete | Replacement requires `--replace-unmeasured --attempt`, preserves link identity, checks task/repository/agent/session identity, refuses measured links, and updates hash plus locator in one `BEGIN IMMEDIATE` transaction. New tests cover identity, locator conflicts, wrong attempts, and attempt-scoped measurements. |
| PR-17 | Documentation and release consolidation | Complete | Version references, TER compatibility, Codex link-versus-score, read-only commands, zero-token semantics, and release checklists were corrected during this review. |
| PR-18 | Statistical improvements | Partial | Deterministic opt-in bootstrap intervals, Brier score, ECE, and reliability bins are implemented and tested. Attempt-scoped outcome pairing and decision-source eligibility diagnostics remain outstanding. |
| PR-19 | Legacy calibration safety | Complete | `calibrate --apply` without `--evidence` requires `--accept-legacy-heuristic`; the flag is rejected in evidence mode. Read-only legacy proposals remain available, and evidence calibration stays non-applying. |

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

- Zero-token legacy aggregation and threshold-proposal safety.
- Null-safe text rendering for linked and legacy reports.
- Read-only `outcomes list`/`export` missing-ledger behavior.
- Codex loaders: response-item messages, malformed envelopes, missing identity,
  conflicting identity, filenames, malformed JSON, and symlinks.
- Codex scoring fail-closed behavior without a TER loader.
- Session replacement: identity, locator conflict, wrong task, measured-attempt
  immutability, and high-level replacement authorization.
- Benchmark version consistency.

