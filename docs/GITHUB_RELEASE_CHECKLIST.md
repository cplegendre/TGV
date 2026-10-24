# GitHub release checklist — TGV 0.39

Run from the repository root. Do not push, tag, or publish until every gate is
green on the target branch.

## 1. Environment

```bash
python --version                 # CI matrix: 3.11, 3.12, 3.13
python -m pip install -e '.[dev,mcp]'
python -m pip install -e '.[ter]'   # optional; required for real TER scoring
```

The TER extra requires network access the first time it is installed or when
the embedding model is not cached.

## 2. Static and contract checks

```bash
ruff check .
ruff format --check .
mypy --strict src/tgv
python scripts/check_version.py
python scripts/check_packaging_cleanliness.py
```

Expected: all pass; version check prints `0.39`.

## 3. Tests

```bash
python -m pytest -q
```

Expected: full pass and coverage at or above the configured 98% gate.

If the loopback HTTP test fails with `PermissionError: Operation not
permitted`, run it outside the restricted sandbox:

```bash
python -m pytest -q tests/test_ollama_http_integration.py
```

This is an environment restriction, not a source defect.

## 4. Routing and benchmark gates

```bash
tgv bench routing --json
tgv bench index --repo . --query "RouteDecision Repository" --json
tgv benchmark run --enforce --json
```

Expected: routing benchmark accuracy gates pass; `benchmark_version` matches
the package version.

## 5. Build and artifact inspection

```bash
rm -rf dist build
python -m build --no-isolation      # use normal isolation in CI
python -m pip install --force-reinstall --no-deps dist/tgv_agent-0.39-py3-none-any.whl
python -c "import tgv; print(tgv.__version__)"
```

Inspect artifacts:

```bash
tar tzf dist/tgv_agent-0.39.tar.gz | grep -E '__pycache__|\.pyc$' && echo FAIL || echo OK
unzip -l dist/tgv_agent-0.39-py3-none-any.whl | grep -E '__pycache__|\.pyc$' && echo FAIL || echo OK
```

Both inspections must report `OK`. Optionally install `check-wheel-contents`
and run it against the wheel.

## 6. Smoke and security checks

```bash
bash scripts/smoke_test.sh
pip-audit
```

Expected: smoke test passes; `pip-audit` reports no known vulnerabilities.
`tgv-agent` itself is skipped by `pip-audit` because it is not on PyPI.

## 7. Repository hygiene

```bash
git status --porcelain=v1
git diff --check
git ls-files | rg -n '\.(env|pem|key|p12|pfx)$|id_rsa|credentials|secret' || true
git grep -n -I -E 'AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' || true
```

Review every untracked file. Do not commit `.tgv/`, `.coverage*`, caches,
virtualenvs, local outcome ledgers, backups, or generated transcripts.

## 8. GitHub Actions review

- `.github/workflows/ci.yml` must use `actions/checkout@v4` and
  `actions/setup-python@v5`.
- CI must pass on Python 3.11, 3.12, and 3.13.
- `ruff format --check .` and `check_packaging_cleanliness.py` must remain in CI.
- Decide whether to activate exactly one CodeQL workflow. The current
  `codeql.yml.backup` and `codeql.yml.disabled` files do not run.
- Confirm repository secrets and branch protection before enabling auto-merge.

## 9. Documentation review

- `README.md`, `CHANGELOG.md`, `RELEASE_NOTES.md`, and `SECURITY.md` agree on
  version 0.39.
- No claim of end-to-end Codex TER scoring appears anywhere.
- TER 3.0.0 unversioned output and `schema_version: 1` are described accurately.
- Read-only commands are documented as non-creating: `report --linked`,
  `outcomes list`, `outcomes export`, `benchmark observed`, `calibrate --evidence`,
  and `confidence`.
- Legacy `calibrate --apply` documentation includes `--accept-legacy-heuristic`.

## 10. Readiness distinctions

- **Review branch:** safe once local gates pass and a human reviews the diff.
- **Merge to `main`:** requires green CI across the supported Python versions
  and a decision on the remaining P2 attempt-atomicity limitation.
- **Versioned GitHub release:** requires the above plus resolution or explicit
  acceptance of TER producer/Codex gaps and the remaining PR-18 items.

Do not equate these levels. Passing local tests alone is not a release.

