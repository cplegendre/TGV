# TGV case study template

> **Synthetic illustration:** The figures below are derived from the repository's routing benchmark shape and telemetry/report fields. They do not represent a production deployment or customer result.

## Scenario

A Python service team evaluates TGV against 100 representative coding requests: repository lookups, localized fixes, clear multi-file changes, and high-risk architecture or security work. Deterministic safety rules remain enabled and telemetry records route metadata without repository excerpts.

## Synthetic results

| Metric | Illustration |
|---|---:|
| Requests evaluated | 100 |
| Local Fast | 38 |
| Local Code | 27 |
| Frontier Standard | 21 |
| Frontier Deep | 14 |
| Local share | 0.65 |
| High-risk safety violations | 0 |
| Aligned tokens | 182,000 |
| Waste tokens | 31,000 |
| Savings tokens | 182,000 |
| Savings ratio | 0.8545 |

The example suggests that 65% of requests could remain on local profiles while all labeled high-risk cases continue to use `frontier_deep`. The savings fields represent non-negative report outputs, not guaranteed monetary savings.

## Evaluation questions

- Did local routes complete accepted work at an adequate success rate?
- Were any denied files or out-of-root paths exposed?
- Did high-risk tasks always escalate?
- Was latency acceptable for each provider?
- Did rejected outcomes improve later experimental scoreboard recommendations?

## Reproduction outline

1. Run `tgv bench routing --json` for deterministic routing evidence.
2. Enable telemetry and route a representative request set.
3. Score sessions with the optional TER integration.
4. Run `tgv report --sessions .tgv/sessions --json`.
5. Record accepted or rejected task outcomes and rerun the experimental scoreboard after the minimum sample threshold is met.
