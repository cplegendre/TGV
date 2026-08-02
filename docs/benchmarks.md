# Local routing and index diagnostics

TGV includes two deterministic, offline diagnostics. They are regression tools for a repository and fixture state, not universal performance claims.

## Routing benchmark

Run `tgv bench routing` or `python -m tgv.bench.routing`. Add `--json` for automation. The committed JSONL corpus contains 76 labeled cases spanning simple extraction, repository inspection, bounded code work, ambiguity, destructive/security/concurrency/architecture risk, privacy constraints, local-only requests, frontier handoff, and conflicting signals.

The report includes exact accuracy, acceptable-route accuracy, a confusion matrix, per-category accuracy, low-confidence correct decisions, high-confidence wrong decisions, and high-risk downgrade violations. CI requires at least 90% acceptable accuracy and zero high-risk violations.

## Index diagnostic

Run `tgv bench index --repo . --query "RouteDecision Repository"` or `python -m tgv.bench.indexing . --json`. The report includes eligible files indexed, skipped files, Python symbols extracted, build latency, search latency, and cache hits/misses.

Numbers depend on filesystem, repository size, cache state, and query. Compare repeated runs on the same fixture or repository rather than treating a small local result as a general benchmark.
