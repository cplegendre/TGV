# Security Policy

## Supported versions

TGV currently supports the **latest published minor line only**. During the `0.x` experimental phase, only the latest release receives security fixes. Older releases may be referenced for historical context but are unsupported.

| Version | Supported |
|---|---|
| 0.32 | Yes |
| < 0.32 | No |

Users should upgrade before reporting a vulnerability unless the issue prevents upgrading.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub private vulnerability reporting when enabled, or contact the maintainers privately. Include affected versions, reproduction steps, impact, and suggested mitigation when possible.

## Security posture

TGV 0.32 is not a sandbox, secret scanner, hardened DLP boundary, or authenticated network service. Repository reads are constrained to a configured root and default deny patterns reduce accidental disclosure risk. Normal CLI operation may persist local indexes, telemetry, and outcome metadata under configured paths. MCP runs in an explicit read-only mode and does not persist those artifacts. Operational status, metrics, indexes, telemetry, and generated context bundles use atomic owner-only writes on POSIX-compatible filesystems where supported.

TGV does not automatically:

- execute arbitrary shell strings;
- modify repository source files;
- download local models;
- transmit context to a hosted provider;
- follow symlinks outside the repository root.

See [docs/threat-model.md](docs/threat-model.md).

## Local indexing and handoff safety

The lexical index stores paths, file metadata, token frequencies, and line locations locally under `.tgv`; it does not require an external service. Default deny patterns omit common environment, package registry, key, credential, token, and secret files. Root-containment and symlink checks apply before reading. External command handoff uses argument arrays with `shell=False`, bounded timeouts, bounded captured diagnostics, and explicit dry-run support. Common credential-bearing command flags are redacted from persisted provider metadata; environment variables remain the preferred way to supply secrets.

These controls reduce accidental exposure. TGV is not a sandbox, data-loss-prevention system, or complete security boundary; users remain responsible for repository permissions, deny-pattern review, and external-agent configuration.
