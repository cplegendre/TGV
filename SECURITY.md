# Security Policy

## Supported versions

TGV currently supports the **latest published minor line only**. During the `0.0.x` experimental phase, this means only the latest release receives security fixes. Older releases may be referenced for historical context but are unsupported.

| Version | Supported |
|---|---|
| 0.28 | Yes |
| < 0.28 | No |

Users should upgrade before reporting a vulnerability unless the issue prevents upgrading.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub private vulnerability reporting when enabled, or contact the maintainers privately. Include affected versions, reproduction steps, impact, and suggested mitigation when possible.

## Security posture

TGV 0.28 is not a sandbox, secret scanner, or hardened DLP boundary. Repository tools are read-only and constrained to a configured root, but users must review context before sending it to any hosted provider.

TGV does not automatically:

- execute shell commands;
- modify repository files;
- download local models;
- transmit context to a hosted provider;
- follow symlinks outside the repository root.

See [docs/threat-model.md](docs/threat-model.md).

## Local indexing and handoff safety

The lexical index stores paths, file metadata, token frequencies, and line locations locally under `.tgv`; it does not require an external service. Default deny patterns omit common environment, package registry, key, credential, token, and secret files. Root-containment and symlink checks apply before reading. External command handoff uses argument arrays with `shell=False`, bounded timeouts, and explicit dry-run support.

These controls reduce accidental exposure. TGV is not a sandbox, data-loss-prevention system, or complete security boundary; users remain responsible for repository permissions, deny-pattern review, and external-agent configuration.
