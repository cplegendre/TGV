# Phase 1 implementation — TGV v0.39

> Historical implementation note. It records the Phase 1 changes; see
> `docs/CODEX_AUDIT.md` and `docs/PR10_PR19_REVIEW.md` for the final verified status.

## PR-12 — Reporting hardening
The supplied source archive already includes PR-12 read-only evidence reads,
missing-ledger handling, quarantined metadata decoding, and focused tests
(`tests/test_pr12_reporting_integrity.py`). Those existing protections are
preserved. No unneeded database migration is introduced.

## PR-10 — Measurement integrity
Claude Code session validation now requires nonempty user *and* assistant text
(including Claude text blocks). Linked TER results with zero accounted tokens
are rejected before persistence. Explicit linking of a second session to an
already-linked attempt is prevented before measurement. Token-weighted
aggregate reporting uses `aggregate_ter: null` with
`efficiency_status: insufficient-token-evidence` when total tokens are zero;
evidence calibration does not treat such populations as efficient.

## PR-11 — Attempt lifecycle
`outcomes complete` now closes pending attempts for the task, assigning
`completed_at` and status `completed`, while preserving non-pending statuses.
The final v0.39 hardening pass made task completion, human feedback, and
pending-attempt closure one SQLite transaction, and added an explicit
`pending -> running/completed/failed` lifecycle with idempotent terminal
updates. This does not synthesize historical provenance for legacy attempts;
historical pending attempts from earlier versions require separate explicit
reconciliation, and unknown legacy statuses are never rewritten automatically.

## Compatibility and limitations
- Previously accepted hollow Claude sessions are rejected.
- Zero-token aggregate values are now null, not 1.0.
- A second distinct session cannot be added to an existing explicitly chosen
  attempt through the high-level linking operation; create a new attempt instead.
- The low-level `link_agent_session` API retains its historical conflict semantics.
- The measurement schema and deterministic routing thresholds are unchanged.
- No TER repository changes were made; TER itself may still return zero-token
  results, which TGV now rejects before persistence on both the linked and
  low-level measurement paths while retaining historical rows read-only.
- Historical note: this phase was originally validated with focused tests only.

## Validation (v0.39 final)
- The full suite passes with coverage at or above the 98% gate.
- `ruff check .`, `ruff format --check .`, and `mypy --strict src/tgv` pass.
- `scripts/check_version.py` reports `0.39`.
- Legacy zero-token aggregation now also reports null TER and cannot lower thresholds.
