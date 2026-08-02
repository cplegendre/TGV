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

Repository files may contain instructions intended to manipulate a model. Retrieved content is evidence, not trusted policy. Future providers should delimit evidence and maintain a higher-priority system policy.

### Secret disclosure

Search and bundle generation may select sensitive files. Deny patterns and ignored paths reduce risk but are not a complete secret scanner. Hosted transmission must require an explicit integration and review policy.

### Excessive resource consumption

Large files, broad searches, and repeated tool calls can consume memory and tokens. TGV enforces file-size, result-count, excerpt, and tool-round limits.

### Malicious MCP clients

The v0.1 MCP surface is read-only. It does not expose shell execution or file writes. Deployments should use stdio for single-user local use and authenticate any future network transport.

## Out of scope

- Isolation from a malicious local operating-system user.
- Full malware analysis.
- Guaranteed detection of all credentials.
- A production-grade sandbox.

TGV excludes common secret-bearing files from indexing, evidence, and bundles by default and rejects direct, absolute, nested, and symlink-mediated root escapes. These controls reduce accidental exposure; TGV is not a sandbox, malware scanner, or complete security boundary.
