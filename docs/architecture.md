# Architecture

TGV is local-first. The router extracts explainable features and produces deterministic scores. The repository layer enforces root containment and privacy rules, maintains a lightweight JSON lexical index under `.tgv`, and falls back to bounded scanning. The compiler creates stable JSON and Markdown handoffs. External coding agents are optional and accessed through a provider abstraction; default operation only writes handoff files.

## TGV and TER boundary

TGV owns routing and outcome semantics: classification, deterministic route selection, safety
policy, context preparation, execution handoff, outcome persistence, verified-success evaluation,
routing benchmarks, evidence-gated calibration, and read-only confidence diagnostics.

TER owns execution measurement: agent-session parsing, token accounting, span classification,
execution-efficiency analysis, and TER scoring. TGV treats TER as a separate executable with a
pinned public CLI contract. It invokes `ter analyze <session.jsonl> --format json`, verifies the
reported version and session identity, and persists only aggregate metrics. TGV does not import
TER scoring internals, share a database with TER, or make TER responsible for routing or
correctness decisions.

This separation means routed tasks, outcomes, and confidence diagnostics remain available when TER
is not installed. See [TER integration](ter-integration.md) for the exact version and JSON
contract, and [TGV and TER end-to-end workflow](tgv-ter-workflow.md) for the complete manual
linking and measurement path.

## Code-aware local index

The lexical index remains the base retrieval layer. Python files additionally receive AST-derived metadata for classes, functions, async functions, methods, safe top-level assignments, and imports. Symbol, import, token, path, proximity, frequency, and extension signals contribute deterministic ranking. Syntax-invalid Python remains searchable lexically.
