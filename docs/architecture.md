# Architecture

TGV is local-first. The router extracts explainable features and produces deterministic scores. The repository layer enforces root containment and privacy rules, maintains a lightweight JSON lexical index under `.tgv`, and falls back to bounded scanning. The compiler creates stable JSON and Markdown handoffs. External coding agents are optional and accessed through a provider abstraction; default operation only writes handoff files.

## Code-aware local index

The lexical index remains the base retrieval layer. Python files additionally receive AST-derived metadata for classes, functions, async functions, methods, safe top-level assignments, and imports. Symbol, import, token, path, proximity, frequency, and extension signals contribute deterministic ranking. Syntax-invalid Python remains searchable lexically.
