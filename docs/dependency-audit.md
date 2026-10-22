# Dependency audit

## TGV 0.32

The local preparation environment does not have `pip-audit`, Ruff, or mypy executables installed, so this archive does not claim those checks were executed locally. The CI workflow remains the authoritative network-enabled dependency and static-analysis gate.

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
