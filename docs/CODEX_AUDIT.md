# TGV 0.39 technical audit

## Executive summary

This audit reviewed the complete TGV v0.39 candidate, with emphasis on
PR-10 through PR-19, the TGV/TER boundary, ledger integrity, read-only
reporting, statistical diagnostics, documentation, packaging, and GitHub
release readiness.

The final hardening pass closed three release-blocking ledger defects:
`outcomes complete` is now one SQLite transaction, execution-attempt status
transitions are validated at the persistence layer, and new TER measurements
are validated before insertion. These changes do not alter routing thresholds,
the outcome schema version, or the TGV/TER architectural boundary.

The local quality gates pass. No P0 issue remains. The remaining P1-class risk
is external: the pinned TER 3.0.0 producer does not emit `schema_version`, has
no Codex loader, and was not changed by this review. TGV correctly fails closed
for Codex scoring and accepts unversioned output only from TER 3.0.0.

## Findings

### P0 — none

No data-loss, authentication bypass, remote-code-execution, or silent
corruption issue was found that blocks all use.

### P1 — release blockers resolved in the final hardening pass

1. **Outcome completion was split across independent commits (fixed).**
   `src/tgv/cli.py` previously called `record_execution`,
   `record_human_feedback`, and `complete_pending_attempts` separately. A
   failure between calls could leave a completed task with pending attempts or
   feedback from only part of the operation.
   Fix: `src/tgv/outcomes.py::OutcomeLedger.complete_outcome` validates the task,
   repository correlation, and attempt correlation, then updates task status,
   human feedback, and all pending attempts under one `BEGIN IMMEDIATE`
   transaction. SQLite failures roll back every write and raise
   `LedgerWriteError`. The CLI output and options are unchanged.
   Regression tests: `tests/test_release_hardening.py`.

2. **Execution-attempt status changes were unrestricted (fixed).**
   `update_execution_attempt` previously accepted any nonempty status string.
   Fix: the ledger now defines `pending`, `running`, `completed`, and `failed`;
   `pending` may move to `running`, `completed`, or `failed`, `running` may move
   to `completed` or `failed`, and terminal states may only repeat themselves.
   Unknown historical statuses are preserved and cannot be automatically
   rewritten. Invalid transitions raise actionable `ValueError` messages before
   an update is committed.
   Regression tests: valid, repeated, invalid, terminal, retry, and legacy-status
   cases in `tests/test_release_hardening.py`.

3. **Low-level TER writes could bypass measurement validation (fixed).**
   `record_ter_measurement` and `record_ter_measurements` previously accepted
   zero-token and internally inconsistent TER metrics even though linked
   measurement rejected zero-token output.
   Fix: `_normalize_metrics` validates every measurement batch before insertion.
   Records containing TER fields must include `aggregate_ter`,
   `aligned_tokens`, and `waste_tokens`; tokens must be non-negative integers;
   `total_tokens`, when present, must equal aligned plus waste; zero-token
   evidence is rejected; and finite TER must be in `[0, 1]`. Generic non-TER
   metric metadata remains supported. Existing historical rows remain readable
   and are never rewritten by this validation.
   Regression tests: `tests/test_release_hardening.py`.

### P1 — open external dependency

4. **TER producer compatibility is not confirmed.** TGV accepts unversioned
   JSON only from TER 3.0.0 and versioned JSON with `schema_version: 1` from
   other TER 3.x builds. The local TER checkout is version 3.0.0 and its
   `src/ter_calculator/formatter_json.py::ter_result_to_dict` emits no
   `schema_version`. The versioned branch is implemented and tested with
   synthetic payloads, but cannot be verified end-to-end against the pinned
   producer. TER's loader parses Claude Code sessions only; no Codex loader was
   found. This is a TER-side dependency, not a TGV implementation gap.

### P2 — significant non-blocking limitations

5. **Calibration pairing is task-scoped, not attempt-scoped (open, PR-18).**
   `src/tgv/calibration.py` joins verified task outcomes with measurements by
   task ID. Repeated attempts are therefore not paired to the specific attempt
   that produced a measurement. This is documented in
   `PHASE3_IMPLEMENTATION.md` and `docs/model-routing.md`.

6. **Bootstrap diagnostics are partial (open, PR-18).**
   `src/tgv/confidence.py::_bootstrap_intervals` provides deterministic
   task-level percentile intervals for overall Brier and ECE only. There are no
   grouped intervals or decision-source eligibility counts. Seed 0 and opt-in
   default behavior are correct, and bootstrap output never feeds routing.

7. **Legacy zero-token history remains readable.** Historical malformed
   measurements are quarantined from aggregation but intentionally preserved in
   the ledger. This is the required compatibility behavior; new writes cannot
   add such records through the public ledger API.

### P3 — hygiene and verification gaps

8. **CodeQL remains disabled (open decision).** Both
   `.github/workflows/codeql.yml.backup` and
   `.github/workflows/codeql.yml.disabled` are inactive. Enabling one is a
   repository security-policy decision.

9. **Remote Python CI remains unverified.** The complete suite and configured
   coverage gate pass locally on Python 3.11.15, 3.12.12, and 3.13.9. CI declares
   the same 3.11/3.12/3.13 matrix, but this review cannot claim remote CI passed;
   GitHub CLI authentication was unavailable.

10. **ResourceWarnings for unclosed SQLite connections remain in tests (open,
    low).** They do not fail the suite, but should be cleaned up in a focused
    test-hygiene change.

11. **No secrets were found.** A source/document scan found no private keys,
    tokens, or machine-specific absolute paths in tracked content.

### Previously fixed in the v0.39 review

- Legacy zero-token route aggregation no longer reports perfect TER or permits
  threshold lowering.
- Text-mode linked reports render unavailable TER as `n/a`.
- `outcomes list` and `outcomes export` no longer create a missing ledger.
- Packaging no longer includes generated `__pycache__`/bytecode files.
- Benchmark metadata now follows the package version.

## TER-side recommendations (not applied)

These changes belong in the TER repository and were not made here:

- `src/ter_calculator/formatter_json.py`: add a stable
  `"schema_version": 1` to analysis JSON and document it.
- Add a tested Codex `rollout-*.jsonl` loader with identity, message, and token
  accounting tests.
- Add a public CLI-contract test that asserts emitted fields and version
  negotiation.
- Publish or tag the compatible producer version so TGV can pin it instead of
  relying on a synthetic contract.

## Verification evidence

Run from the repository root:

- Python 3.13.9 full suite: 623 passed, 2 skipped in 35.97s; coverage 98.04%
  (98% gate). The skips are optional MCP and TER-extra integration tests not
  installed in the primary review environment.
- Python 3.11.15 and 3.12.12 full suites: 624 passed, 1 skipped in 26.99s and
  27.09s respectively. Configured coverage runs pass at 98.04%; the remaining
  skip is the optional TER-extra integration test.
- `ruff check .`: passes.
- `ruff format --check .`: 124 files formatted.
- `mypy --strict src/tgv`: passes, 38 files.
- `python scripts/check_version.py`: `0.39`.
- `python scripts/check_packaging_cleanliness.py`: passes.
- `python -m build --no-isolation`: builds sdist and wheel; no bytecode entries.
- `bash scripts/smoke_test.sh`: passes.
- `tgv bench routing --json`, `tgv bench index ...`, and
  `tgv benchmark run --enforce --json`: pass.
- `pip-audit --skip-editable`: no known vulnerabilities.
- Wheel and sdist inspections exclude bytecode, virtual environments, SQLite
  databases, credentials, and local environment files.
- The loopback HTTP integration test passes outside the restricted sandbox. The
  sandbox blocks local socket binding with `PermissionError`; this is an
  environment restriction, not a source defect.

## Release-readiness assessment

- Ready to push to a review branch: **yes**, after the changes in this working
  tree are reviewed and committed by a human.
- Ready to merge to `main`: **conditionally**, subject to green CI on Python
  3.11-3.13 and human review. Local suites pass on all three versions; the local
  atomicity, lifecycle, and measurement blockers are resolved.
- Ready for a versioned GitHub release: **conditionally**, after CI evidence and
  explicit acceptance of the TER producer/Codex and remaining PR-18 limitations.
