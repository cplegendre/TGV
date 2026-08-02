# TGV 0.29 release notes

TGV 0.29 adds scalable deterministic demo runs while preserving the fast 450-task installation check.

Use `tgv demo --scale medium` for 5,000 tasks, `--scale large` for 25,000, or `--scale stress` for 100,000. `tgv demo --tasks N` accepts any positive explicit count and overrides the selected preset.

Expanded runs cycle through the canonical 450 labeled tasks with deterministic unique task identifiers. They demonstrate routing throughput, telemetry isolation, reporting, and repeatability; they are not additional independent benchmark cases or evidence of production routing quality. No artificial delay, network access, provider call, API key, Ollama instance, or TER dependency is required.

Human and JSON summaries now include the selected scale, canonical-task count, elapsed routing time, and tasks per second. The stable `savings_ratio` machine field remains backward compatible.
