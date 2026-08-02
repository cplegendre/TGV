# Dependency audit

## TGV 0.29

The release environment could not install `pip-audit` because its configured package index returned no distributions. The available environment check, `python -m pip check`, reported one environment-level conflict unrelated to TGV: installed `moviepy 2.2.1` requires Pillow `<12`, while the shared sandbox has Pillow `12.2.0`. TGV does not depend on either package, so no TGV dependency change is warranted.

CI should run the following authoritative audit in a network-enabled environment:

```bash
python -m pip install -e '.[dev]'
pip-audit
```

TGV directly depends on bounded major versions of `httpx`, `pydantic`, and `PyYAML`. Optional MCP and TER dependencies remain opt-in. A successful dependency audit is a release responsibility; absence of a finding in this document is not a security guarantee.
