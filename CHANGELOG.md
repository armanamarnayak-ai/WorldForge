# Changelog

## 0.2.0 — 2026-10-17

- Static/local MCP tool-schema compiler with explicit synthetic bindings, output schemas,
  preserved descriptions/provenance, bounded parsing, and compatibility reporting.
- Versioned deterministic failure specifications for virtual delays, timeouts, lost responses,
  duplicate delivery, stale reads, denial, rate limits, unavailable services, and response corruption.
- Bounded Failure Oracle search, deletion minimization, repeated verification, self-contained
  Failure Certificates, and CLI inspection/reproduction linked to replayable runs.
- Honest refund-retry adversarial example that detects a changed idempotency key while retaining
  the existing ledger's conservation and idempotency guarantees.
- Resource Observatory reports with measurement provenance, warnings, provider usage metadata,
  explicitly configured cost estimates, and aggregate summaries.
- Provider-neutral contracts and a deterministic local mock adapter; no provider API key required.
- Namespaced composed worlds, explicit cross-service relationships, and a minimal synthetic
  email fixture using the shared simulator and runtime.
- Schema `1.1` extensions with frozen released `1.0` schemas and compatibility for legacy run replay.

Integration and Failure Discovery, extending the released v0.1.0 foundation.
Dashboard work and all v0.3/v0.4 systems remain deferred.

## 0.1.0 — 2026-10-14

- Typed, versioned world, scenario, state, event, run, and evaluation contracts.
- OpenAPI subset compiler with declared/inferred provenance and compatibility reporting.
- Stateful synthetic payments world with refund constraints and idempotency.
- Seeded duplicate-payment scenarios and deterministic failure injection.
- Reference agent and trusted Python adapter protocol with step and timeout limits.
- Explicit evaluation, usage measurements, local run storage, and deterministic action replay.
- CLI, example specifications and adapters, tests, packaging, documentation, and GitHub CI.

Initial public release of the local OpenAPI-to-evaluation-and-replay workflow.
