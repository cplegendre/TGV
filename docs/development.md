# Development

Use Python 3.11 or newer.

```bash
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
mypy --strict
python -m build
bash scripts/smoke_test.sh
```

CI runs the same gates on Python 3.11, 3.12, and 3.13. Tests should exercise behavior and branch conditions rather than adding superficial coverage-only assertions.

## Release diagnostics

Run `python scripts/check_version.py`, `tgv bench routing --json`, and `tgv bench index --repo . --json` before a release. Benchmarks are deterministic local regression diagnostics, not vendor or hardware performance guarantees.
