# Release-candidate review — TGV 0.39

## Scope

Independent repository review of the v0.39 candidate on branch
`fix/pr10-measurement-integrity`. The review covered PR-10 through PR-19, the
TGV/TER boundary, measurement and ledger integrity, read-only behavior,
statistical diagnostics, documentation, packaging, and GitHub release
readiness.

No TER repository files were modified, and no commits were pushed.

## Verification summary

- Full suite: 582 passed, 2 skipped, with coverage at 98.03% (98% gate). The
  skips are optional MCP and TER-extra integration tests not installed locally.
- `ruff check .`: passes.
- `ruff format --check .`: passes.
- `mypy --strict src/tgv`: passes.
- `python scripts/check_version.py`: reports `0.39`.
- `python scripts/check_packaging_cleanliness.py`: passes on the prospective
  archive file set.
- The loopback HTTP integration test requires permission to bind a local
  socket. It fails inside the restricted sandbox and passes outside it; this is
  an environment limitation, not a source defect.

Exact commands are recorded in `docs/GITHUB_RELEASE_CHECKLIST.md`.

## Defects fixed in this review

1. **Legacy zero-token aggregation.** `aggregate_by_route` no longer returns
   `aggregate_ter: 1.0` when a route has no accounted tokens. It reports null
   TER and `efficiency_status: insufficient-token-evidence`, and
   `propose_thresholds` refuses to lower a threshold from zero-token evidence.
2. **Null-safe reporting.** Text-mode `report --linked` and `efficiency` render
   unavailable TER as `n/a` instead of raising a formatting error.
3. **Read-only outcome inspection.** `outcomes list` and `outcomes export` now
   use a read-only ledger and do not create `.tgv/outcomes.sqlite3`.
4. **Type and packaging hygiene.** Corrected strict-mypy failures, added the
   optional MCP import override, excluded pytest coverage temp files from git,
   and made the packaging check evaluate the prospective archive file set.

## Outstanding limitations

- TER-side Codex loading and schema-version production remain outstanding.
- Attempt-scoped outcome pairing and decision-source eligibility diagnostics
  are not implemented.
- CLI `outcomes complete` performs several separately committed writes; a
  failure between them can leave partial state.
- Legacy calibration remains heuristic and now requires an explicit
  acknowledgement for writes.

See `docs/CODEX_AUDIT.md` for severity-ranked findings and
`docs/PR10_PR19_REVIEW.md` for per-PR status.
