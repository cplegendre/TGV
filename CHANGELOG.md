## [0.29] - 2026-07-29

### Added

- Added deterministic demo scale presets: `small` (450), `medium` (5,000), `large` (25,000), and `stress` (100,000).
- Added `tgv demo --tasks N` for an explicit positive task count.
- Added elapsed-time and routing-throughput metrics to human-readable and JSON demo summaries.

### Changed

- Large demo runs expand the canonical 450-task corpus deterministically without network or model calls.
- CLI demo runs avoid retaining per-task result objects while preserving isolated normal-format telemetry.

## [0.28] - 2026-07-29

### Changed

- Renamed the human-readable demo label from “Estimated savings ratio” to “Estimated avoided frontier-token share”.
- Formatted the human-readable metric as a percentage while retaining the public `savings_ratio` JSON and report field for backward compatibility.
- Clarified the metric definition in onboarding and release documentation.
- Added the `types-tqdm` development stub dependency and retained strict mypy compatibility.
- Updated release metadata and supported-version documentation for version 0.28.

## [0.27] - 2026-07-29

### Changed

- Expanded the deterministic synthetic demo from 50 to 450 individually routed tasks.
- Added a terminal-aware `tqdm` progress bar for human-readable demo runs.
- Replaced verbose per-task output with aggregate PASS/FAIL percentages and report metrics.
- Kept JSON mode machine-readable and free of progress or decorative output.
- Updated packaging, tests, documentation, and release metadata for version 0.27.

## [0.26] - 2026-07-29

### Changed

- Standardized release numbering from `0.0.xx` to `0.xx`.
- Expanded the deterministic synthetic demo from 10 to 50 tasks with balanced coverage of all four routing profiles.
- Updated version validation, packaging fixtures, tests, and documentation for the `0.xx` scheme.

- Add a fully local deterministic synthetic demo through `tgv --demo` and `tgv demo`.
- Isolate demo telemetry, outcome data, repository indexes, and reports from user configuration.
- Package the synthetic repository and task fixtures in wheels and source distributions.

## [0.24] - 2026-07-29

### Fixed (post-release hardening pass)
- Fixed `tests/test_version_consistency.py` failing under plain `pytest` (missing
  root `conftest.py` for the `scripts` import — same class of bug as 0.22/0.23).
- Fixed `tgv report` requiring the optional TER dependency to run at all; it now
  works out of the box using existing telemetry token estimates, with `--ter`
  as an opt-in for the more precise TER-scored aligned/waste report.
- Fixed 3 `mypy --strict` errors: an unbound-method call with a mismatched
  `self` type in `providers.py`, an `int(object)` overload error in
  `daemon.py`, and a reused-variable type conflict in `cli.py`.
- Fixed `ruff check` / `ruff format` findings across `router.py`,
  `test_providers.py`, `test_v015_features.py`, and `test_v024_features.py`
  (line length, import order/placement).
- Updated `docs/getting-started.md` to reflect the TER-free default.

### Added

- Added latest-minor-only support policy and primary day-one onboarding guide.
- Added OpenAI-compatible local provider support for `/v1/chat/completions`.
- Added explicit adaptive cold-start fallback reasons and outcome completion feedback.
- Added sequential stdio serving with structured logs, atomic status files, timeouts, and graceful signals.
- Added CI-stable value reporting and a synthetic case-study illustration.

### Changed
- Marked adaptive scoreboard and calibration behavior as experimental.
- Preserved deterministic routing, repository confinement, deny patterns, and absolute high-risk escalation.

## [0.21] - 2026-07-28

### Fixed

- Fixed lint and formatting regressions introduced by the 0.20 feature
  additions (evaluation, intelligence, outcomes, scoreboard modules): `ruff
  check .` and `ruff format --check .` now pass with zero findings again.
  No behavioral changes.

## [0.20] - 2026-07-28

### Added
- Structured two-stage task profiling with bounded complexity dimensions and explicit uncertainty.
- Privacy-minimal SQLite outcome ledger with schema versioning and sanitized JSON/CSV export.
- Local lexical and structural code retrieval facade with token budgeting and explainable scores.
- Bayesian-smoothed routing scoreboard with minimum sample thresholds and deterministic fallback.
- Integrated `profile`, `retrieve`, `scoreboard`, `outcomes`, and `benchmark run` CLI workflows.

### Changed
- Routing now attaches revised task profiles and historical recommendations while preserving mandatory safety escalation.
- Context handoffs include profiling and scoreboard evidence.

## [0.19] - 2026-07-28

- Removed a stray `src/tgv/providers.py.tmp` scratch file and stale generated
  artifacts (`.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`,
  `src/tgv_agent.egg-info/`) that had been accidentally included in the
  distributed archive.
- Added `tests/test_packaging_cleanliness.py` to fail the build if any
  cache, temp, or generated artifact is present in the source tree.
- Broadened `scripts/check_version.py` to detect version drift across
  `CHANGELOG.md`, `ROADMAP.md`, `SECURITY.md`, `examples/requests.txt`, and
  `docs/dependency-audit.md`.

## [0.18] - 2026-07-28

- Brought the codebase to a fully clean state under its own CI gates:
  `ruff check .` / `ruff format --check .` pass with zero findings, and
  `mypy --strict` passes with zero errors on `src/tgv`.
- Wrapped `Router.WEIGHTS` in `MappingProxyType` to prevent accidental
  mutation of shared routing weights.

## [0.17] - 2026-07-28

- Added a 76-case labeled routing benchmark with human-readable and JSON reports.
- Added AST-based Python symbol metadata and explainable code-aware ranking.
- Added routing and index benchmark CLI commands and CI regression gates.
- Added provider lifecycle metadata, bounded diagnostics, and optional result ingestion.
- Expanded end-to-end CLI, security, version-consistency, and index tests.
- Corrected archive naming and documentation/version drift.

## [0.16] - 2026-07-28

- Harden repository root confinement against traversal and symlink escapes.
- Raise branch-aware test coverage above 98%, including real Ollama HTTP timeout/retry tests.
- Add efficiency benchmark and dependency-audit documentation.
- Refresh README and package version metadata.

# Changelog

All notable changes are documented here. The project follows Keep a Changelog and uses Semantic Versioning while the public API stabilizes.

## [0.14] - 2026-07-28

- Ran `ruff format .` to actually reformat the files that had been
  flagged since v0.7 (models.py, cli.py, compiler.py, config.py,
  efficiency.py, telemetry.py, and several test files). No behavior
  changes; formatting only.
- Verified `ruff check .`, `ruff format --check .`, `mypy src/tgv`, and
  `pytest -q --cov=tgv` all pass clean.
- No other changes.

## [0.13] - 2026-07-28

- Fixed the `ter-install` workflow dependency to reference the existing `quality-gate` job.
- Applied Ruff formatting and lint cleanup, including `tests/test_router.py` and import ordering in `src/tgv/config.py`.

## [0.12] - 2026-07-28

- Reset the maintained code line to the exact v0.10 behavior baseline before making changes.
- Added one blocking CI quality-gate job that runs pytest with coverage, Ruff lint, Ruff formatting verification, and strict mypy before TER installation, matrix tests, or smoke tests.
- Added the contributor requirement to run `make format && make check` and paste the output into the pull-request description.
- Added test-only coverage for repository symlink and binary handling, local-model routing fallback and confidence boundaries, and CLI error paths.
- Reviewed README, architecture, and release history to remove stale references to the withdrawn v0.11 quality expansion.
- No routing, provider, CLI, configuration-schema, telemetry, or repository runtime behavior changes relative to v0.10.

## [0.11] - 2026-07-28

- Withdrawn from the maintained line because it added unrequested logging, benchmarking, Bandit, coverage-policy, and Makefile scope beyond the approved baseline.
- Superseded by v0.12, which starts from the exact v0.10 behavior boundary.

## [0.10] - 2026-07-28

- Restored the release to the behavior boundary of v0.7, removing the unreviewed v0.8 outcome-capture work and v0.9 routing, doctor, and prepare-output changes.
- Retained only the approved v0.7 cleanup: Ruff lint/format corrections, removal of the unused shipped index protocol, and TER efficiency/calibration documentation.
- Re-established the formatter workflow through pre-commit, separate CI lint and format checks, and the `make format` command.
- Rewrote the changelog in strict reverse-chronological order.
- No router, provider, efficiency, telemetry, doctor, or prepare behavior changes relative to v0.7.

## [0.9] - 2026-07-28

- Withdrawn from the maintained line because it introduced unreviewed routing and CLI behavior beyond the cleanup-only scope.
- Superseded by v0.10.

## [0.8] - 2026-07-28

- Withdrawn from the maintained line because it introduced unreviewed outcome-capture behavior beyond the cleanup-only scope.
- Superseded by v0.10.

## [0.7] - 2026-07-28

- Restored Ruff lint and formatter cleanliness after the v0.6 review.
- Removed the unused `RepositorySearchIndex` protocol from the shipped package; the indexing direction remains documentation-only.
- Documented opt-in TER telemetry, route-efficiency reporting, and dry-run calibration behavior in the README and architecture documentation.
- Clarified telemetry configuration comments in the example configuration.
- No routing, provider, efficiency, telemetry, doctor, or prepare behavior changes.

## [0.6] - 2026-07-28

- Fixed the example configuration and improved duplicate-key diagnostics.
- Kept extractive security questions local while preserving true high-risk escalation.
- Added provider lifecycle management, richer route explanations, and expanded doctor output.
- Documented bounded full-scan search and added outcome and frontier-handoff model foundations.

## [0.5] - 2026-07-28

- Corrected the optional TER dependency pin to the verified `v3.0.0` tag.
- Added isolated CI installation validation for `tgv[ter]`.
- Restored strict Ruff lint and format enforcement.
- Added `tgv calibrate` with dry-run, minimum-sample, and explicit apply behavior.
- Added a checked-in route-efficiency regression baseline and required CI gate.

## [0.4] - 2026-07-28

- Added opt-in, privacy-minimal TER-compatible session recording.
- Added route tags that survive TER loading through an unscored user metadata block.
- Added the optional `ter` dependency pinned to TER v3.0.0.
- Added `tgv efficiency` text and JSON reports grouped by routing decision.
- Added telemetry, aggregation, CLI, privacy, and TER-loader regression tests.

## [0.3] - 2026-07-28

- Completed Ruff lint and formatter cleanup.
- Corrected context-compiler evidence typing for strict mypy compatibility.
- No runtime behavior changes.

## [0.2] - 2026-07-28

- Enforced Ruff, formatting, mypy, pre-commit, and CI quality gates.
- Hardened Ollama configuration, timeouts, retries, and deterministic fallback behavior.
- Expanded repository, CLI, routing, smoke, and MCP integration coverage.
- Added configurable routing patterns and `tgv route --explain`.
- Raised the coverage floor to 85%.

## [0.1] - 2026-07-27

- Added the initial local-first routing architecture.
- Added the Ollama model adapter with fast and code roles.
- Added the deterministic offline router.
- Added read-only repository tools and the context-bundle compiler.
- Added the optional FastMCP stdio server.
- Added the CLI, tests, CI, governance, and security documentation.

## 0.15

- Replaced regex-only routing precedence with deterministic, explainable feature scoring while retaining mandatory high-risk escalation.
- Added an incremental local lexical repository index with safe eligibility checks, invalidation, stable ranking, and scan fallback.
- Expanded context bundles to schema version 2 with scores, assumptions, questions, omissions, validation, and repository metadata.
- Added optional safe command-based coding-agent handoff with `handoff-only`, `command`, and `disabled` modes.
- Added script-friendly CLI JSON output, dry-run handoff support, stronger sensitive-file defaults, and Python 3.11–3.13 CI.
