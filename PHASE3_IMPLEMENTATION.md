# TGV 0.39 — Phase 3 implementation (PR-16–PR-19)

## PR-16 — Explicit relinking of an unmeasured session

`outcomes link TASK_ID --session ... --agent ... --attempt ATTEMPT_ID --replace-unmeasured` validates the new file and atomically updates its content hash and locator **only when** the existing attempt contains exactly one linked session, the agent/session identity is unchanged, the repository matches, and no TER measurement exists for the attempt or link. The existing session-link ID is preserved. A measured link remains immutable; create a new attempt if its source changes. New snapshots are *not* retained. The new locator replaces the old one, so operators should minimize retention of absolute paths. Default linking behavior is unchanged.

## PR-17 — Documentation and release consolidation

Release notes and README are updated for version 0.39. `efficiency --linked` can persist measurements; `report --linked`, `confidence`, and observational diagnostics remain inspection paths. TGV supports Python >=3.11; the pinned TER extra has a narrower compatibility envelope. Codex validation is available, **but TER-backed Codex scoring remains unsupported** until TER ships the loader and producer contract.

## PR-18 — Opt-in statistical uncertainty

`confidence --bootstrap-samples N` (N=1..10000) adds deterministic task-level nonparametric bootstrap percentile 95% intervals for Brier and ECE. Seed fixed to 0. No bootstrap runs by default. This is exploratory descriptive uncertainty; pooled different decision sources remain heterogeneous, samples are not independent across all workflows, and intervals do not establish causal effects or correct for multiple comparisons. Attempt-scoped pairing and source-ineligibility counts are not part of this incremental release.

## PR-19 — Legacy calibration write guard

`calibrate --apply` without `--evidence` now requires `--accept-legacy-heuristic`. This is intentionally a breaking safety guard for legacy heuristic threshold writes; read-only proposal generation is unchanged and the evidence-gated mode remains non-applying. No legacy implementation was removed in this release.

## Validation and limits

- The full suite and the 98% coverage gate pass; see `docs/GITHUB_RELEASE_CHECKLIST.md`.
- `ruff check .`, `ruff format --check .`, and `mypy --strict src/tgv` pass.
- Added regression coverage for replacement guards, Codex validation failures, and
  null-safe zero-token reporting.
- No changes to the TER repository, routing defaults, or outcome schema version.
