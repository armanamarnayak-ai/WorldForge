# Roadmap

These are directions for review, not available commands, release dates, or promises. v0.1 must
remain a working, testable foundation before implementation of later milestones begins.

## v0.1.0 — Foundation and payments vertical slice

OpenAPI subset → versioned world IR → stateful synthetic payments → seeded scenarios → agent
protocol → explicit evaluation → local event history → deterministic replay. Includes a reference
agent, CLI, failure injection, tests, documentation, and clean-install packaging validation.
The implementation has been reviewed, CI-verified, and merged into `main`. Historical review
and validation evidence is preserved in [docs/v0.1-review.md](docs/v0.1-review.md).

## v0.2 — Integration and failure discovery

Potential work includes MCP input/adapters, richer failure models, an initial Failure Oracle with
reproducible failure certificates, multi-service foundations, model/provider adapters, resource
reporting, and a minimal developer dashboard. Each needs a scoped design and validation plan.

## v0.3 — Reality Distillation and fidelity

Potential work includes explicit, privacy-conscious ingestion of sanitized OpenTelemetry, traces,
HAR, and logs; versioned reality profiles; calibration; and fidelity measurement with transparent
methodology. Input sanitization and consent must precede use of real telemetry.

## v0.4 and later — EVOLVE and learning workflows

Potential work includes adaptive scenario generation, curricula, failure-driven search, exported
datasets, training integrations, and larger composed worlds. Evidence of benefit, reproducibility,
resource budgets, and evaluation methodology should drive scope.

Current extension boundaries are recorded in [ARCHITECTURE.md](ARCHITECTURE.md). None of these
future systems is implemented merely by appearing on this roadmap.
