# TGV 0.39 release notes

Phase 3 adds opt-in replacement of unmeasured session links (PR-16), release-documentation updates (PR-17), optional bootstrap confidence intervals (partial PR-18), and an explicit safety acknowledgement for legacy calibration writes (PR-19). Attempt-scoped outcome pairing and decision-source eligibility diagnostics remain outstanding. See `PHASE3_IMPLEMENTATION.md` and `docs/RELEASE_REVIEW.md` for validation limits.

The v0.39 review pass fixed two measurement-integrity defects: legacy zero-token route
aggregation no longer reports perfect TER or permits threshold lowering, and text-mode linked
reports render unavailable TER safely. `outcomes list` and `outcomes export` no longer create a
missing ledger.

The final release-hardening pass makes `outcomes complete` atomic across task status, human
feedback, and pending-attempt closure. Execution-attempt updates now enforce explicit lifecycle
transitions while preserving historical statuses and retry workflows. Public TER measurement
writes reject zero-token, negative, non-integral, inconsistent-total, non-finite, and out-of-range
values before persistence; existing historical rows remain readable and are not rewritten.
Full verification results and remaining limitations are in `docs/CODEX_AUDIT.md`,
`docs/PR10_PR19_REVIEW.md`, and `docs/GITHUB_RELEASE_CHECKLIST.md`.

Phase 2 integrates PR-15 (TGV half of a versioned TER JSON contract), PR-14 (Codex linking), and guards for PR-13 (TER-side Codex loader, **not included**).

The existing TER 3.0.0 unversioned JSON contract is still accepted. Newer TER 3.x CLI builds must emit `schema_version: 1` in their JSON analyses. Unsupported schema versions, TER major versions, and unversioned newer output fail closed. TER 4 has a distinct architecture and is not declared compatible.

Codex `rollout-*.jsonl` files can now be explicitly linked via `--agent codex` when they contain a stable session_meta identity and nonempty user and assistant text. The legacy TER analyzer cannot interpret these files: linked Codex measurement fails with an actionable error instead of persisting a misleading zero-token result. A validated TER-side Codex loader is required for automated scoring.

No TER repository modifications are included in this TGV archive.
