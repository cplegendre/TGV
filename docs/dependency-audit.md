# Dependency audit

## TGV 0.39

The v0.39 review environment executed the local checks: `ruff check .`,
`ruff format --check .`, `mypy --strict src/tgv`, and `pip-audit` all pass
after refreshing the development environment to current patched releases.
The CI workflow remains the authoritative network-enabled dependency and
static-analysis gate for clean installs across Python 3.11-3.13.

CI should run the following authoritative audit in a network-enabled environment:

```bash
python -m pip install -e '.[dev,mcp]'
pip-audit
```

TGV directly depends on bounded major versions of `httpx`, `pydantic`, `PyYAML`, and `tqdm`. Optional MCP and TER dependencies remain opt-in. A successful dependency audit is a release responsibility; absence of a finding in this document is not a security guarantee.

The optional `ter` extra pins the verified TER 3.0.0 tag and requests its
`embeddings` extra so real TER analysis has the required `sentence-transformers`
dependency. Installing the Python dependencies does not guarantee that the
embedding model is cached: the first TER analysis may require a model download.
See [TER integration](ter-integration.md) for a runtime smoke-test procedure.
