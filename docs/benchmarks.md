# Local routing and index diagnostics

TGV includes two deterministic, offline diagnostics. They are regression tools for a repository and fixture state, not universal performance claims.

## Routing benchmark

Run `tgv bench routing` or `python -m tgv.bench.routing`. Add `--json` for automation. The committed JSONL corpus contains 76 labeled cases spanning simple extraction, repository inspection, bounded code work, ambiguity, destructive/security/concurrency/architecture risk, privacy constraints, local-only requests, frontier handoff, and conflicting signals.

The report includes exact accuracy, acceptable-route accuracy, a confusion matrix, per-category accuracy, low-confidence correct decisions, high-confidence wrong decisions, and high-risk downgrade violations. CI requires at least 90% acceptable accuracy and zero high-risk violations.


## Integrated routing-intelligence benchmark

Run `tgv benchmark run --enforce --json` to evaluate task categorization, complexity bands, deterministic route acceptability, and false-low-risk behavior together. TGV 0.32 commits 104 curated synthetic cases covering all 13 task categories, with 32 low-, 56 medium-, and 16 high-complexity cases.

The release gate requires at least 100 cases, 90% task-category accuracy, 90% complexity-band accuracy, 95% acceptable-route accuracy, and a 0% false-low-risk rate. CI enforces the gate. These cases are intentionally broader than the 76-case low-level deterministic routing corpus, but remain a regression corpus rather than a claim about all real-world repositories or tasks.

## Index diagnostic

Run `tgv bench index --repo . --query "RouteDecision Repository"` or `python -m tgv.bench.indexing . --json`. The report includes eligible files indexed, skipped files, Python symbols extracted, build latency, search latency, and cache hits/misses.

Numbers depend on filesystem, repository size, cache state, and query. Compare repeated runs on the same fixture or repository rather than treating a small local result as a general benchmark.

## Observed outcome benchmark

Run `tgv benchmark outcomes --repo . --json` after a deployment has recorded outcomes. Unlike the curated release corpus, this diagnostic reads the repository-scoped privacy-minimal SQLite ledger and summarizes verified success, human acceptance, rework, retries, safety observations, latency, and estimated cost by model/tier/category.

`--enforce` defaults to at least 20 verified observations, at least 85% verified success, and zero observed safety violations. An undersampled ledger fails an enforced gate as insufficient evidence. Do not compare these rates across repositories without accounting for task mix and validation coverage.
