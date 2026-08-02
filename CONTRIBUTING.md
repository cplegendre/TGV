# Contributing

Thank you for contributing to TGV.

## Development workflow

1. Create or reference an issue for non-trivial work.
2. Fork the repository or create a feature branch.
3. Keep changes focused and add tests.
4. Install hooks once with `pre-commit install`. Before every commit, run `make format`, followed by `make check` and `make build`.
5. Open a pull request using the template.
6. Address review feedback with follow-up commits; maintainers may squash on merge.

## Branch and commit conventions

- Default branch: `main`.
- Branches: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Prefer Conventional Commit subjects, for example `feat(router): add risk override`.
- Do not commit secrets, proprietary source, local model files, indexes, or generated bundles.

## Pull-request checklist

- Before opening a PR, run `make format && make check` and paste the output in the PR description.

## Pull-request expectations

A pull request should explain the problem, approach, security/privacy impact, tests, and compatibility impact. Changes to routing policies must include representative cases showing both local and frontier routes.

## Design principles

- Local first, but quality first.
- Evidence must remain traceable to source paths and ranges.
- The original user request must not be silently replaced.
- Models request actions; policy-controlled code executes them.
- Read-only by default.
- Provider and model names remain configuration, not architecture.
- Savings are measured per successful task.

## Developer Certificate of Origin

By contributing, you certify that you have the right to submit the work under the Apache-2.0 license. Sign commits with `git commit -s` where required by project policy.

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Pre-commit hooks

After installing development dependencies, enable the repository hooks:

```bash
pre-commit install
pre-commit run --all-files
```

The hooks run Ruff linting with automatic fixes, Ruff formatting, mypy, and basic file validation. The low-friction manual equivalent for formatting is `make format`, which runs `ruff check --fix .` followed by `ruff format .`. CI independently runs `ruff check .` and `ruff format --check .` so an unformatted tree cannot pass the quality job.
