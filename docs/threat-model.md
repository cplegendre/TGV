# Threat Model

## Assets

- Source code and documentation.
- Credentials accidentally stored in a repository.
- Developer intent and task history.
- Integrity of routing and context selection.

## Threats

### Path traversal and symlink escape

A tool argument may attempt to access files outside the configured repository. TGV resolves paths, checks containment, and rejects symlinks that escape the root.

### Prompt injection in repository content

Repository files may contain instructions intended to manipulate a model. Retrieved content is evidence, not trusted policy. Providers should delimit evidence and maintain a higher-priority system policy.

### Secret disclosure

Search and bundle generation may select sensitive files. Deny patterns and ignored paths reduce risk but are not a complete secret scanner. Hosted transmission must require an explicit integration and review policy. External command metadata redacts common credential-bearing flags, but operators should pass secrets through environment variables rather than command arguments.

### Excessive resource consumption

Large files, broad searches, repeated requests, noisy subprocesses, and large ignored trees can consume resources. TGV enforces file-size/result/excerpt limits, prunes ignored directories before descent, bounds retained subprocess diagnostics, and applies request deadlines in the stdio daemon. Routing runs in a persistent isolated worker process started with `spawn` on every supported platform. When a request exceeds its deadline, TGV terminates that worker before returning the timeout; the timed-out work cannot continue in a background thread. A clean worker is created for the next request. During SIGTERM/SIGINT shutdown, TGV stops accepting new requests, marks readiness as draining, allows the current request a configurable grace period, and then terminates the worker if necessary.

### Local state disclosure and partial writes

Operational state can reveal repository paths, route choices, model identifiers, or task metadata even when source contents are excluded. TGV 0.32 writes status, Prometheus metrics, repository indexes, telemetry files, and context bundles atomically and uses owner-only permissions on POSIX-compatible filesystems where supported. Operators should still protect the `.tgv` directory and any configured external state paths.

### Outcome-store concurrency

Multiple local processes can contend on the SQLite outcome ledger. TGV uses WAL mode and a busy timeout to reduce transient writer contention. This is not a substitute for a network database if high write concurrency becomes a requirement.

### External agent-session linking

`tgv outcomes link` accepts an explicit session path because real coding-agent transcripts often
live outside the repository. TGV rejects missing paths, directories, final-component symlinks,
non-regular files, oversized files, malformed records, missing or inconsistent session identifiers,
repository mismatches, and conflicting or changed links. It stores the agent name, external session
identifier, SHA-256 content hash, attempt correlation, timestamps, and an **absolute session file
locator**. Raw transcript contents, prompts, responses, and classified spans are not copied into the
ledger. The locator is sensitive metadata because it can reveal usernames, project names, and
directory structure; it is retained so later linked TER analysis can revalidate the exact file.
TGV does not discover sessions or scan home directories, and it validates only documented session
formats. Only `claude-code` JSONL is currently supported. See
[TGV and TER end-to-end workflow](tgv-ter-workflow.md#persistence-and-privacy).

Session files are opened by descriptor with `O_NOFOLLOW` where supported, and `fstat()` applies the regular-file and size checks to the opened object. Parent-directory symlinks are followed, only the final component is protected, platforms without `O_NOFOLLOW` retain a narrow pre-check race, and concurrent in-place file modification is not locked. The content hash reflects the bytes read from the opened descriptor rather than a stable snapshot guarantee.

### Malicious MCP clients

The v0.32 MCP surface is read-only. It does not expose shell execution or repository-source writes, and MCP service mode suppresses index, outcome, and telemetry persistence. Deployments should use stdio for single-user local use and authenticate any future network transport.

## Out of scope

- Isolation from a malicious local operating-system user.
- Full malware analysis.
- Guaranteed detection of all credentials.
- A production-grade sandbox.

TGV excludes common secret-bearing files from indexing, evidence, and bundles by default and rejects direct, absolute, nested, and symlink-mediated root escapes. These controls reduce accidental exposure; TGV is not a sandbox, malware scanner, or complete security boundary.
