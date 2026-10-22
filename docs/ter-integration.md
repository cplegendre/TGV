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
directories, symbolic links, oversized files, and malformed session records, and verifies that
the session contains a consistent session identifier with at least one user and one assistant
message. It stores only the agent name, the external session identifier, the SHA-256 content
hash, the attempt link, and timestamps. The absolute path and raw session contents are never
stored. Repeating an identical link is idempotent; conflicting links and changed content are
rejected.

The first link reuses an existing empty attempt or creates one. Linking a different session to a
task that already has a linked attempt creates the next attempt. Use `--attempt ATTEMPT_ID` to
target an existing attempt explicitly. TGV never discovers sessions automatically and never scans
developer home directories.

### Supported session formats

- `claude-code`: JSONL records with `type` set to `user` or `assistant`, a `message` object, and a
  consistent `sessionId`. This is the format parsed by TER 3.0.0.

Codex exposes a current session identifier through hooks and a transcript path, but official
OpenAI documentation does not define a stable transcript-file schema. TGV therefore does not
validate or link Codex transcript files yet. Adding a future format requires a documented schema
and agent-specific validation tests.

## Record validation evidence

```bash
tgv outcomes validate TASK_ID --tests-passed --static-checks-passed --repo .
tgv outcomes validate TASK_ID --tests-failed --repo .
```

Positive and negative evidence can be recorded independently. Contradictory flags are rejected.
Validation evidence uses the existing outcome semantics: a completed task with no tests, static
checks, or human feedback remains unverified. Task completion and human acceptance commands are
unchanged.

## Interpret results carefully

TGV's built-in telemetry records request text and a **synthetic routing reply**,
not the downstream coding agent's full session. Running TER on this telemetry
measures the synthetic messages, **not real coding-agent execution efficiency**.
Linking a real session records durable metadata only; TER scoring of linked
sessions is not implemented in this change.
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

This integration documents the pinned TER 3.0.0 CLI contract and fixes optional
installation requirements. It also documents TGV's explicit metadata-only session linking and
validation-evidence commands. It does not run TER during linking, implement TER scoring or linked
efficiency reports, update routing algorithms, or support an unverified TER 4 API.
