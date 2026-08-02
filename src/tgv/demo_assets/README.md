# TGV synthetic demo

This repository, task set, telemetry, and report output are synthetic illustrations intended only to demonstrate TGV workflows. They are not evidence of production savings, routing quality, or model performance.

The sample repository is dependency-free and exists solely for local installation verification.

## Coverage scope

The canonical demo contains 450 deterministic synthetic tasks: 120 `local_fast`, 120 `local_code`, 105 `frontier_standard`, and 105 `frontier_deep`. The default `small` run processes those tasks once. The `medium` (5,000), `large` (25,000), and `stress` (100,000) presets deterministically cycle the canonical corpus with unique synthetic task identifiers; `--tasks N` selects a custom positive count. The CLI prints aggregate PASS/FAIL, elapsed time, throughput, and report metrics. Expanded runs demonstrate scalability and repeatability, but they are not additional independent benchmark cases or evidence of production performance.
