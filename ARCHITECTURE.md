# Architecture decisions — v0.1

WorldForge is a local modular monolith. The first release proves OpenAPI → typed world →
stateful payments simulation → agent → evaluation → event history → deterministic replay.

1. **Language and package:** Python 3.11+, a `src/worldforge` package, no server or dashboard.
   Modules follow working boundaries: models, compiler, simulation, scenarios, agents,
   runtime, storage, and CLI. No directories for unimplemented roadmap features.
2. **Contracts:** Pydantic v2, version `1.0`, JSON/YAML compatible. Extra fields fail validation.
   Resource/action provenance records declared versus inferred information without invented
   confidence numbers. Checked-in JSON schemas are exported from these models.
3. **Compiler:** safe YAML/JSON loading, bounded inputs, local acyclic references only.
   An explicit subset of OpenAPI 3 is compiled into action argument and resource schemas.
   `x-worldforge` annotations declare executable semantics. Unannotated CRUD inference is
   visible, and unsupported operations remain inspectable rather than silently executable.
   JSON Schema validation uses the maintained `jsonschema` library instead of a partial clone.
4. **State:** per-run in-memory resource dictionaries; actions produce validated state
   transitions. Integer minor currency units avoid floating point money. Generic CRUD and
   the explicitly declared payments refund rule are the v0.1 behavior set.
5. **Events:** ordered typed events describe observations, decisions, calls, responses,
   failures, transitions, and completion. Event timestamps use a logical clock for replay;
   run creation time and measured elapsed time are actual wall-clock metadata.
6. **Persistence:** self-contained versioned JSON artifacts, atomically written through a
   small run-store interface. A run embeds its world, scenario, actions, outcomes, and initial
   state. SQLite/PostgreSQL may implement the same boundary later; neither is needed today.
7. **CLI:** Typer exposes only functioning import/inspect/generate/run/replay operations.
   Files live in an explicitly selected local workspace (default `.worldforge`).
8. **Tests:** pytest unit/integration/CLI tests and Hypothesis refund invariants; Ruff and
   strict mypy; build and clean-wheel installation checks in CI. Tests require no model key.
9. **Determinism and replay:** a private seeded RNG generates scenarios. Simulator IDs and
   failure occurrence counters are deterministic. Replay executes recorded tool actions,
   verifies results, state, evaluation, and event structure; it never calls the original agent.
   External model outputs are recordings, not claims of reproducible model inference.
10. **Agent boundary:** an async protocol receives visible observations and tool contracts,
    returns actions or a final message, and has reset/shutdown hooks. The runtime limits
    steps and awaits agents with timeouts. Provider metadata and optional usage measurements
    stay outside the simulator. No hidden chain-of-thought is requested or stored.
11. **Security boundary:** simulation has no HTTP client and never executes OpenAPI servers,
    shell commands, or remote references. Parsing and scenario budgets are bounded. Custom
    Python adapters are trusted executable code, require explicit opt-in, and are **not**
    sandboxed. Isolation of hostile agents needs external process/container controls.
12. **Future boundaries:** MCP is another compiler/adapter input; Reality Distillation can
    produce versioned worlds/failure configurations after local sanitization; EVOLVE can
    consume runs/evaluations and emit scenarios. None is implemented in v0.1. Independent
    run state and store interfaces leave room for batch workers without global mutable state.
13. **License:** Apache-2.0 permits reuse and includes an explicit patent grant, appropriate
    for infrastructure intended for research and commercial integrations.

The simulation is synthetic. An OpenAPI interface does not establish real business semantics
or fidelity, and this release makes no production readiness or benchmark claims.

