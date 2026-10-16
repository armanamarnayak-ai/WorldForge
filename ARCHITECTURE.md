# Architecture decisions

WorldForge is a local modular monolith. The released v0.1.0 foundation proves OpenAPI → typed
world → stateful payments simulation → agent → evaluation → event history → recorded-action replay.
The v0.2 development branch extends this loop with local MCP compilation, richer failures,
resource reporting, provider contracts, composition, and Failure Certificates.

1. **Language and package:** Python 3.11+, a `src/worldforge` package, local CLI, and no server or
   dashboard. Module boundaries follow implemented behavior rather than speculative subsystems.
2. **Contracts:** Pydantic v2 with extra fields rejected. World IR remains the shared compiler
   output. Composed worlds, richer scenarios, and new runs use deliberately versioned `1.1`
   extensions. Released `1.0` schemas remain frozen; old run serialization and replay semantics
   remain supported. Certificates/search/resource reports have independent `1.0` schemas.
3. **Compilers:** `compiler.py` accepts the documented OpenAPI subset; `mcp.py` accepts static
   local tool documents. Both use bounded safe parsing, offline acyclic references, explicit
   provenance, and compatibility issues. Interfaces do not establish business semantics:
   executable MCP behavior requires `x-worldforge` bindings, and limited OpenAPI CRUD inference
   remains visibly inferred. Schema validation uses `jsonschema` with no network retrieval.
4. **State and failures:** `simulation.py` owns independent per-run resource tables, validated
   transitions, logical steps, occurrence counters, and a virtual latency clock. Refund semantics
   retain integer minor units, declared invariants, and stable idempotency. Faults alter observable
   delivery or valid transitions without bypassing state constraints; virtual delay never sleeps.
5. **Composition:** `composition.py` namespaces actions/resources and retains declared provenance.
   Explicit relationships connect service-owned state through the shared runtime. Namespaced
   payments and a tiny email fixture prove the boundary; this is not a distributed service emulator.
6. **Agents and providers:** `agents.py` preserves the async `AgentAdapter` protocol. `providers.py`
   exposes vendor-neutral identities, observable decisions, typed usage, and optional pricing.
   The local mock wraps the reference policy. Provider records are adapter reports, not independently
   audited billing. No hidden model chain-of-thought is requested or persisted.
7. **Runtime and evaluation:** `runtime.py` bounds cooperative callbacks and tool steps, records
   observable actions/results/state, and evaluates the actual ledger. New evaluations flag changed
   idempotency keys after ambiguous responses as unsafe attempts. Legacy run replay retains the
   original evaluation policy rather than rewriting historical judgments.
8. **Resources:** `resources.py` derives counters and warnings from recorded calls and typed
   provider metadata. Each metric carries availability, source, unit, and provenance. Missing
   token, cost, or quota information stays unavailable; estimated cost requires explicit pricing.
9. **Failure Oracle:** `oracle.py` enumerates a finite payments scenario family under candidate,
   run, time, and runtime budgets; applies an explicit criterion; performs deletion minimization;
   and repeatedly verifies the result. `certificates.py` validates evidence links and stores a
   self-contained certificate. One-minimality is conditional on completed deletion checks and
   the tested seed, not a claim of a globally minimal adversary.
10. **Events and replay:** logical ordering covers observations, decisions, calls, injected faults,
    simulated latency, provider/resource reports, warnings, state, and completion. A separate
    typed search audit covers discovery and minimization. Replay verifies recorded simulator
    behavior and available deterministic resource metrics; it never regenerates external inference.
    Creation timestamps, artifact IDs, and actual elapsed time remain nondeterministic metadata.
11. **Persistence and CLI:** local JSON run/certificate stores are bounded and atomically written.
    Artifacts embed the information needed to replay or explicitly reproduce the failure. Loading
    a certificate does not execute stored adapter paths. CLI operations consume real artifacts.
12. **Security:** compilers and simulator do not execute production API/MCP calls, shell commands,
    or remote schemas. Python adapters/providers are explicitly trusted code, **not sandboxed**.
    Async deadlines do not forcibly stop hostile or blocking code. Artifacts may contain sensitive
    data; automatic scrubbing and cryptographic attestation are not implemented.
13. **Validation:** pytest unit/integration/CLI tests, adversarial inputs, replay checks, and Hypothesis
    invariants support the existing Ruff/strict-mypy/build/wheel quality bar. The reference path
    requires no provider internet or credentials; remote CI results must be observed separately.
14. **Scope:** no dashboard, live MCP networking, real telemetry ingestion, fidelity calibration,
    Capability Genome, EVOLVE, curricula, training, self-modification, production execution,
    or cloud SaaS implementation. The modular boundaries permit future review without creating
    placeholder implementations now. The license remains Apache-2.0.

Detailed contracts and migration implications are in [docs/v0.2-architecture.md](docs/v0.2-architecture.md).
The simulation remains synthetic: neither an interface schema nor a repeatable failure proves
fidelity to a real service, production readiness, or model performance beyond the tested scenario.
