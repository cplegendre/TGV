# Release-candidate review — TGV 0.39

## Scope

Independent repository review of the v0.39 candidate on branch
`fix/pr10-measurement-integrity`. The review covered PR-10 through PR-19, the
TGV/TER boundary, measurement and ledger integrity, read-only behavior,
statistical diagnostics, documentation, packaging, and GitHub release
readiness.

No TER repository files were modified, and no commits were pushed.

## Verification summary

- Python 3.13.9 full suite: 623 passed, 2 skipped in 35.97s, with coverage at
  98.04% (98% gate). The skips are optional MCP and TER-extra tests not
  installed in the primary environment.
- Python 3.11.15 and 3.12.12 full suites: 624 passed, 1 skipped each; both pass
  the configured 98.04% coverage gate. The remaining skip is the optional TER
  extra test.
- `ruff check .`: passes.
- `ruff format --check .`: passes; 124 files formatted.
- `mypy --strict src/tgv`: passes; 38 source files.
- `python scripts/check_version.py`: reports `0.39`.
- `python scripts/check_packaging_cleanliness.py`: passes.
- The loopback HTTP integration test requires permission to bind a local socket.
  It fails inside the restricted sandbox and passes outside it; this is an
  environment limitation, not a source defect.

Exact commands are recorded in `docs/GITHUB_RELEASE_CHECKLIST.md`.

## Release-blocking defects fixed

1. **Atomic outcome completion.** `OutcomeLedger.complete_outcome` validates the
   task and attempt correlation, then commits task completion, human feedback,
   and pending-attempt closure under one `BEGIN IMMEDIATE` transaction. Injected
   failures in each sub-operation leave the ledger unchanged.
2. **Execution-attempt lifecycle.** `update_execution_attempt` enforces a
   deterministic transition graph, permits idempotent terminal updates, and
   preserves unknown historical statuses rather than rewriting them.
3. **Persistence-level measurement integrity.** Public measurement writes reject
   zero-token evidence, negative or non-integral token counts, inconsistent
   totals, non-finite TER, and TER outside `[0, 1]`. Historical rows remain
   readable and unmodified.

## Previously fixed in this review

4. **Legacy zero-token aggregation.** `aggregate_by_route` no longer returns
   `aggregate_ter: 1.0` when a route has no accounted tokens. It reports null
   TER and `efficiency_status: insufficient-token-evidence`, and
   `propose_thresholds` refuses to lower a threshold from zero-token evidence.
5. **Null-safe reporting.** Text-mode `report --linked` and `efficiency` render
   unavailable TER as `n/a` instead of raising a formatting error.
6. **Read-only outcome inspection.** `outcomes list` and `outcomes export` use a
   read-only ledger and do not create `.tgv/outcomes.sqlite3`.
7. **Type and packaging hygiene.** Corrected strict-mypy failures, added the
   optional MCP import override, excluded coverage temp files from git, and made
   the packaging check evaluate the prospective archive file set.

## Outstanding limitations

- TER-side Codex loading and schema-version production remain outstanding.
- Attempt-scoped outcome pairing and decision-source eligibility diagnostics
  are not implemented.
- Bootstrap intervals remain opt-in, task-level, and descriptive only.
- Legacy calibration remains heuristic and requires explicit acknowledgement
  for writes.
- Remote CI on Python 3.11-3.13 has not been verified in this session.

See `docs/CODEX_AUDIT.md` for severity-ranked findings and
`docs/PR10_PR19_REVIEW.md` for per-PR status.
