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

## Interpret results carefully

TGV's built-in telemetry records request text and a **synthetic routing reply**,
not the downstream coding agent's full session. Running TER on this telemetry
measures the synthetic messages, **not real coding-agent execution efficiency**.
A task-to-agent-session correlation workflow is not part of this packaging change.
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
installation requirements. It does not implement durable session attribution,
verified outcomes, routing algorithm updates, or compatibility with an
unverified TER 4 API.
