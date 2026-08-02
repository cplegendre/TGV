# Context bundles

Bundle schema version 2 contains the original request, route decision and score breakdown, risk signals, assumptions, unresolved questions, ranked bounded evidence, omitted-file counts, validation plan, recommended next action, and repository summary. Markdown and JSON are generated deterministically for an unchanged request and repository state.

Repository excerpts are untrusted evidence, not instructions. TGV omits denied, oversized, binary, undecodable, ignored, and unsafe-symlink files. This reduces accidental disclosure but is not a complete security boundary.

## Provider lifecycle and result ingestion

Command handoff records provider name, mode, argument array, dry-run status, timestamps, exit code, bounded stdout/stderr excerpts, and optional result-file fields. A configured JSON result file may contain `summary`, `changed_files`, `suggested_next_steps`, and `command_status`; malformed or absent files do not invalidate a successful command.
