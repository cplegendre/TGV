# TER integration contract (TGV 0.32)

TGV optionally runs [TER](https://github.com/cplegendre/TER) as a separate process to
analyze session JSONL files. The supported, repository-verified TER package is
`ter-calculator` **3.0.0**, pinned to the Git tag `v3.0.0`. Manuscript tags `v4`
and `v5` must not be treated as TER package releases without separate verification.

## Install

From a TGV checkout:

```bash
python -m pip install -e '.[ter]'
```

Or, from an environment that can install TGV's published package:

```bash
python -m pip install 'tgv-agent[ter]'
```

The optional dependency requests `ter-calculator[embeddings]` because TER's
analysis requires an embedding backend provided by `sentence-transformers`.
This is an opt-in dependency; the base TGV installation remains lightweight.
A fresh first analysis may download a model, requiring network access. In offline
environments, pre-cache the embedding model according to TER's documentation.

## Verify the CLI and analysis contract

Confirm the installed executable and inspect its version:

```bash
ter --version
```

With an **actual supported agent session** JSONL file, run:

```bash
ter analyze /path/to/agent-session.jsonl --format json
```

Check that the JSON result contains `session_id`, `aggregate_ter`,
`aligned_tokens`, `waste_tokens`, and `total_tokens`. TGV's adapter currently
consumes only `aggregate_ter`, `aligned_tokens`, and `waste_tokens`, and ignores
extra fields such as `classified_spans`. This CLI JSON shape is protected by
TGV adapter tests but is not a guarantee of compatibility with future TER
versions. A real smoke test requires a compatible session file and an available
embedding model; fake-executable unit tests cannot establish that end-to-end
integration succeeds.

Use `tgv efficiency --sessions PATH` to score sessions that TGV accepts.
Missing executables, invalid TER JSON, and execution failures produce TGV errors.

## Link a real agent session to a task

TGV can durably associate an explicit agent-session file with an existing outcome task and
execution attempt. Linking is a metadata operation; it does not run TER.

```bash
tgv outcomes link TASK_ID \
  --session ~/.claude/projects/<project>/<session>.jsonl \
  --agent claude-code \
  --repo .
```

The command validates that the task belongs to the current repository, rejects missing files,
directories, endpoint symbolic links, oversized files, and malformed session records, and verifies
that the session contains a consistent session identifier with at least one user and one assistant
message. It stores only the agent name, the external session identifier, the SHA-256 content hash,
the attempt link, an explicit absolute file locator, and timestamps. Raw session contents, prompts,
responses, classified spans, and transcripts are never stored. Repeating an identical link is
idempotent; conflicting links, conflicting file locators, and changed content are rejected.

Session files are opened by descriptor with `O_NOFOLLOW` where the platform supports it, and the
regular-file and size checks run against `fstat()` for the file actually opened. The size limit is
also enforced while reading, so a file that grows after opening is rejected. Symlinks in parent
directories are followed because the operating system traverses the explicit path; only the final
path component is protected from symlink substitution. On platforms without `O_NOFOLLOW`, TGV uses
a pre-open symlink check, which retains a narrow check-to-open race. TGV does not lock session
files: concurrent in-place modification can produce a hash of the bytes actually read, and
replacing the path after opening does not redirect reads away from the opened descriptor.

Attempt selection and session linking execute in one `BEGIN IMMEDIATE` SQLite transaction. The
first link reuses an existing empty attempt or creates one; if linking fails, the attempt is rolled
back. Linking a different session to a task that already has a linked attempt creates the next
attempt. Use `--attempt ATTEMPT_ID` to target an existing attempt explicitly. TGV never discovers
sessions automatically and never scans developer home directories.

### Supported session formats

- `claude-code`: JSONL records with `type` set to `user` or `assistant`, a `message` object, and a
  consistent `sessionId`. This is the format parsed by TER 3.0.0.

## Measure linked sessions

After one or more sessions have been linked and outcome validation evidence has been recorded, run:

```bash
tgv efficiency --linked --repo .
```

Linked mode loads attempts and session links from the outcome ledger, revalidates each session file,
compares its external session identity and SHA-256 fingerprint with the stored link, writes the
exact validated bytes to an owner-only temporary snapshot, and invokes the TER CLI against that
snapshot. The snapshot is removed after success or failure, so later path replacement or in-place
changes cannot alter the analyzed content. TGV also verifies the TER executable version through
the public `ter --version` interface, requires TER 3.0.0, compares the `session_id` returned by
TER with the linked session, and rejects out-of-range or internally inconsistent aggregate token
metrics. It persists only aggregate TER metrics in `ter_measurements`. It never scores TGV's
synthetic telemetry.

All linked sessions are validated and analyzed before measurements are committed in one SQLite
transaction. A TER failure or measurement conflict therefore rolls back the batch. Re-running an
identical measurement is idempotent; conflicting analyzer results raise an error rather than
overwriting stored evidence. The `--linked` and `--sessions` options cannot be combined.

View persisted measurements without invoking TER:

```bash
tgv report --linked --repo .
```

The linked report separates raw measurement records from token-weighted aggregates by route,
selected model, and capability tier. It reports total, linked, measured, verified-success, and
unverified task counts separately. A task that completed without positive validation evidence is
not counted as a verified success. Missing, changed, or identity-mismatched session files are
rejected during measurement.

Measurement history is preserved, but aggregation is deterministic: for each linked session, TGV
uses only the most recent valid measurement and marks earlier measurements as superseded in the
raw record list. Ties are broken by measurement identifier. This prevents multiple analyzer
versions from counting the same session more than once.

Codex exposes a current session identifier through hooks and a transcript path, but official
OpenAI documentation does not define a stable transcript-file schema. TGV therefore does not
validate or link Codex transcript files yet. Adding a future format requires a documented schema
and agent-specific validation tests.

## Record user-attested validation evidence

```bash
tgv outcomes validate TASK_ID --tests-passed --static-checks-passed --repo .
tgv outcomes validate TASK_ID --tests-failed --repo .
```

Positive and negative evidence can be recorded independently. Contradictory flags are rejected.
These flags record results the user attests to; TGV does not execute tests or static checks and
cannot independently confirm the claims. Validation evidence uses the existing outcome semantics:
a completed task with no tests, static checks, or human feedback remains unverified. Task
completion and human acceptance commands are unchanged.

## Interpret results carefully

TGV's built-in telemetry records request text and a **synthetic routing reply**,
not the downstream coding agent's full session. Running TER on this telemetry
measures the synthetic messages, **not real coding-agent execution efficiency**.
Linking a real session records durable metadata only; TER scoring of linked
sessions is available through `tgv efficiency --linked`; the two modes must not
be compared as equivalent measurements.
Do not treat synthetic TER scores as execution efficiency, verified correctness,
or proven token savings. Historical threshold calibration based on synthetic
telemetry has the same limitation.

## Troubleshooting and privacy

- `ter` not found: activate the environment where `tgv-agent[ter]` was installed;
  verify `ter` is on `PATH`.
- Missing `sentence-transformers`: reinstall the TER extra, then inspect the
  environment for conflicting dependency installations.
- Model download fails: check network access or pre-cache the required model.
- Invalid JSON or unsupported session: verify that the file is a TER-compatible
  JSONL session and that the CLI prints the expected top-level fields.
- Slow startup: initial embedding-model initialization can be expensive.

Session files can contain sensitive prompts, tool outputs, and source code.
Handle them as sensitive artifacts. TER's full JSON output can contain raw text
in detailed fields; do not publish it indiscriminately. TGV's narrow adapter
keeps only aggregate values and does not need detailed span content.

## Scope

This integration documents the pinned TER 3.0.0 CLI contract, optional
installation requirements, metadata-only session linking, validation evidence,
and linked TER measurement. It does not run TER during linking, update routing
algorithms, or support an unverified TER 4 API.
