# Phase 2 implementation status (TGV v0.39)

> Historical implementation note. TER-side PR-13 remains outstanding; see
> `docs/PR10_PR19_REVIEW.md` for the final status table.

- PR-15 (TGV): typed schema_version negotiation, schema 1 support, legacy 3.0.0 compatibility, incompatible output rejection. **TER-side producer modification remains pending.**
- PR-14 (TGV): `--agent codex` with rollout JSONL validation, session_meta identity, hash/size/symlink checks, meaningful text requirement.
- PR-13 (TER): **not implemented** (TER repository was not supplied as a local working tree). Codex scoring is explicitly blocked with the built-in adapter rather than mis-scored.
- TER 4 is not assumed CLI-compatible.

Please merge a corresponding TER JSON schema_version change and independently tested Codex tokenizer/loader in TER before enabling Codex analysis in production.
