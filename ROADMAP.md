# Roadmap

This document distinguishes the released foundation, current development, and future directions.
Future entries are not available commands, release dates, or promises. v0.1.0 remains the stable
foundation; its tag is immutable.

## v0.1.0 — Foundation and payments vertical slice

OpenAPI subset → versioned world IR → stateful synthetic payments → seeded scenarios → agent
protocol → explicit evaluation → local event history → deterministic replay. Includes a reference
agent, CLI, failure injection, tests, documentation, and clean-install packaging validation.
The implementation has been reviewed, CI-verified, and released as v0.1.0. Historical review
and validation evidence is preserved in [docs/v0.1-review.md](docs/v0.1-review.md).

## v0.2 — Integration and failure discovery (development)

The `0.2.0.dev0` branch implements static/local MCP compilation, typed deterministic faults,
bounded payments-family Failure Oracle search and deletion minimization, repeated verification,
versioned inspectable/reproducible Failure Certificates, provenance-bearing resource reports,
provider-neutral mock adapters, and explicit namespaced composition with a small email fixture.
It extends existing IR/runtime/replay contracts with deliberate compatibility notes. See
[the v0.2 review](docs/v0.2-review.md) for actual validation evidence and limitations.

Dashboard work is deferred. Live MCP networking, real-provider SDK integrations, and a broad
service catalog remain future integration work. This development branch is not a v0.2 release.

## v0.3 — Reality Distillation and fidelity

Potential work includes explicit, privacy-conscious ingestion of sanitized OpenTelemetry, traces,
HAR, and logs; versioned reality profiles; calibration; and fidelity measurement with transparent
methodology. Input sanitization and consent must precede use of real telemetry.

## v0.4 and later — EVOLVE and learning workflows

Potential work includes adaptive scenario generation, curricula, failure-driven search, exported
datasets, training integrations, and larger composed worlds. Evidence of benefit, reproducibility,
resource budgets, and evaluation methodology should drive scope.

Reality Distillation, real telemetry/log/HAR ingestion, fidelity calibration, Capability Genome,
EVOLVE, adaptive curricula, RL/training, autonomous self-modification, production API execution,
and cloud SaaS are not implemented in the v0.2 scope. Current extension boundaries are recorded
in [ARCHITECTURE.md](ARCHITECTURE.md). Future systems are not implemented merely by appearing
on this roadmap.
