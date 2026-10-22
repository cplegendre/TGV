# TGV Development Guidelines

## Project objectives

TGV is a task-routing and execution-orchestration system.

Its purpose is to select an appropriate execution profile
while preserving task quality and minimizing unnecessary cost.

TER is the independent measurement system used to evaluate
execution efficiency.

## Architectural constraints

1. Preserve separation between routing and measurement.

2. TGV must not reimplement TER's efficiency algorithms.

3. Prefer stable public TER interfaces over internal imports.

4. Preserve deterministic routing behavior by default.

5. Changes to routing policy require regression tests.

6. Never optimize token consumption at the expense of
   verified task correctness.

7. Maintain backward compatibility unless explicitly
   authorized to introduce a breaking change.

8. Do not introduce unnecessary external dependencies.

9. Do not silently modify security constraints.

10. Do not change routing thresholds without evidence.

## Development requirements

- Use Python 3.11-compatible syntax.
- Follow existing repository patterns.
- Include tests for new functionality.
- Run relevant tests before declaring completion.
- Run Ruff and mypy.
- Run the routing benchmark when routing behavior changes.
- Report test failures and unverified behavior explicitly.

## Pull request requirements

Each PR must include:

- Problem statement
- Implementation summary
- Files changed
- Compatibility considerations
- Test results
- Known limitations

## Scope control

Implement only the requested improvement.

Do not perform unrelated refactoring.

If a larger architectural change becomes necessary,
document the reason before implementing it.
