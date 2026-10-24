# TGV 0.39 technical audit

## Executive summary

This audit reviewed the complete TGV v0.39 candidate, with emphasis on
PR-10 through PR-19, the TGV/TER boundary, ledger integrity, read-only
reporting, statistical diagnostics, documentation, packaging, and GitHub
release readiness.

The candidate is functionally testable and the local verification gates now
pass. Three correctness defects were confirmed and fixed: legacy zero-token
aggregation claimed perfect efficiency, text reporting could crash on a null
linked TER value, and `outcomes list`/`outcomes export` created a missing
ledger. Packaging also shipped demo-asset `__pycache__` files and reported a
stale benchmark version; both were fixed.

No P0 issue remains. The remaining P1-class risk is external: the pinned TER
3.0.0 producer does not emit `schema_version`, has no Codex loader, and has not
been changed by this review. TGV correctly fails closed for Codex scoring.

## Findings

### P0 — none

No data-loss, authentication bypass, remote-code-execution, or silent
corruption issue was found that blocks all use.

### P1 — release-relevant correctness and integrity

1. **Legacy zero-token aggregation claimed perfect efficiency (fixed).**
   `src/tgv/efficiency.py::aggregate_by_route` returned
   `aggregate_ter: 1.0` for routes with zero aligned and waste tokens, and
   `waste_ratio()` returned `0.0`. With enough sessions this could make
   `propose_thresholds()` lower confidence thresholds on no evidence.
   Reproduction: `aggregate_by_route([ScoredSession("local_fast", 0.2, 0, 0)])`.
   Fix: zero-token routes now return `aggregate_ter: null` and
   `efficiency_status: insufficient-token-evidence`; proposals use
   `insufficient_data` and leave the threshold unchanged.
   Regression tests: `tests/test_production_hardening.py`,
   `tests/test_efficiency.py`.

2. **Linked text report crashed on null TER (fixed).**
   `src/tgv/cli.py::_render_linked_report` formatted
   `metrics["aggregate_ter"]` with `:.4f`. `_aggregate()` deliberately returns
   `None` when total tokens are zero, so `tgv report --linked` (and
   `efficiency --linked`) could raise `TypeError` in text mode.
   Fix: render `n/a` for unavailable TER, including the legacy route table.
   Regression test: `tests/test_pr12_reporting_integrity.py`.

3. **Read-only outcome inspection created a ledger (fixed).**
   `outcomes list` and `outcomes export` entered `TGVService`, whose write-mode
   `OutcomeLedger` initializes the database on first query. An isolated temp
   repository gained `.tgv/outcomes.sqlite3` after merely listing outcomes.
   Fix: both actions now use `OutcomeLedger(..., read_only=True)` before service
   construction and return an empty export when the ledger is absent.
   Regression test: `tests/test_pr12_reporting_integrity.py`.

### P2 — significant but non-blocking limitations

4. **CLI `outcomes complete` is not one transaction (open).**
   `src/tgv/cli.py` performs `record_execution`, `record_human_feedback`, then
   `complete_pending_attempts` as three commits. A failure between steps can
   leave a completed task with a still-pending attempt. The individual ledger
   operations are atomic and idempotent; a ledger-level completion method would
   close the gap.

5. **`update_execution_attempt` accepts arbitrary statuses (open).**
   `src/tgv/outcomes.py` validates only nonempty status. It does not enforce a
   transition graph. This is a low-level API; high-level completion preserves
   non-pending historical statuses.

6. **Calibration pairing is task-scoped, not attempt-scoped (open, PR-18).**
   `src/tgv/calibration.py` joins verified task outcomes with measurements by
   task ID. Repeated attempts are therefore not paired to the specific attempt
   that produced a measurement. This is acknowledged in
   `PHASE3_IMPLEMENTATION.md` and `docs/model-routing.md`.

7. **TER producer compatibility is not confirmed (open, TER-dependent).**
   TGV accepts unversioned JSON only from TER 3.0.0 and versioned JSON with
   `schema_version: 1` from other TER 3.x builds. The local TER checkout is
   version 3.0.0 and its
   `src/ter_calculator/formatter_json.py::ter_result_to_dict` emits no
   `schema_version`. Thus TGV's versioned branch is implemented and tested with
   synthetic payloads but cannot be verified end-to-end against the pinned
   producer. TER's loader parses Claude Code sessions only; no Codex loader was
   found.

8. **Zero-token measurements can still be persisted through the low-level API (mitigated).**
   `record_ter_measurement(s)` accepts `aligned_tokens=0, waste_tokens=0`.
   The linked measurement path rejects such results before persistence, and
   aggregates now report null TER, so evidence is not misinterpreted. The
   low-level API is intentionally generic and should stay documented as such.

9. **Bootstrap diagnostics are partial (open, PR-18).**
   `src/tgv/confidence.py::_bootstrap_intervals` provides deterministic
   task-level percentile intervals for overall Brier and ECE only. There are no
   grouped intervals or decision-source eligibility counts. Seed 0 and opt-in
   default behavior are correct, and bootstrap output never feeds routing.

10. **The wheel shipped generated bytecode (fixed).**
    `MANIFEST.in` recursively included `src/tgv/demo_assets`, which packaged
    local `__pycache__/*.pyc` files. Fix: global exclusion of `*.py[cod]`,
    `*.pyo`, and `__pycache__`. Rebuilt wheel and sdist contain zero bytecode
    files.

### P3 — hygiene and documentation

11. **Benchmark metadata reported 0.32 (fixed).** `src/tgv/evaluation.py` now
    uses `tgv.__version__`; a consistency test was added.
12. **Documentation contained stale 0.32 claims (fixed).** README, SECURITY,
    threat model, operations, model routing, benchmarks, routing intelligence,
    TER integration, and the TER workflow now describe 0.39 behavior.
13. **Packaging check scanned virtualenvs (fixed).** The script now checks the
    prospective archive file set (git-tracked plus non-ignored files) and
    supports `--root` for extracted archives.
14. **CodeQL remains disabled (open decision).** Both
    `.github/workflows/codeql.yml.backup` and `.github/workflows/codeql.yml.disabled`
    are inactive. Enabling one is a repository security-policy decision.
15. **Python 3.11 was not executed locally (verification gap).** The local
    environment is Python 3.13.9. CI declares a 3.11/3.12/3.13 matrix, but this
    session could not certify those interpreters.
16. **ResourceWarnings for unclosed SQLite connections remain in tests (open,
    low).** They do not fail the suite, but should be cleaned up.
17. **No secrets were found.** A source/document scan found no private keys,
    tokens, or machine-specific absolute paths in tracked content.

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

Run from `/home/legendre/Bureau/Codes/TGV` with Python 3.13.9:

- Full suite: 582 passed, 2 skipped in 23.53s; coverage 98.03% (gate 98%).
  The skips are the optional MCP and TER-extra integration tests because those
  extras were not installed in the local review environment.
- `ruff check .`: passes.
- `ruff format --check .`: 123 files formatted.
- `mypy --strict src/tgv`: passes, 38 files.
- `python scripts/check_version.py`: `0.39`.
- `python scripts/check_packaging_cleanliness.py`: passes.
- `python -m build --no-isolation`: builds sdist and wheel; no `.pyc` entries.
- `bash scripts/smoke_test.sh`: passes.
- `tgv bench routing --json`, `tgv bench index ...`, and
  `tgv benchmark run --enforce --json`: pass.
- `pip-audit`: no known vulnerabilities after refreshing local tooling.

The loopback HTTP integration test requires local socket binding. It fails in
the restricted sandbox with `PermissionError` and passes outside it; this is an
environment restriction, not a code failure.

## Release-readiness assessment

- Ready to push to a review branch: **yes**, after the changes in this working
  tree are reviewed and committed by a human.
- Ready to merge to `main`: **conditionally**, subject to CI on Python
  3.11-3.13 and resolution of the P2 attempt-atomicity question.
- Ready for a versioned GitHub release: **not yet**. TER-side producer/Codex
  work, Python-version CI evidence, and the remaining PR-18 items must be
  resolved or explicitly accepted and documented.

