# WorldForge v0.1.0

**Give your AI a world to learn in before giving it yours.**

WorldForge is an open-source artificial experience engine for stateful synthetic environments
where autonomous agents can be evaluated and their tool interactions replayed.

WorldForge v0.1.0 is a local Python library and CLI. Its working vertical slice compiles
a documented OpenAPI subset, simulates a payments world, generates seeded duplicate-payment
scenarios, runs a reference or custom agent, evaluates state changes, and saves replayable runs.
The reference agent needs no model account, API key, or paid service.

```text
OpenAPI document → typed world → stateful simulation → agent
                                                     ↓
                                              evaluation + replay
```

## Try the payments example

Requires Python 3.11 or newer on Linux or macOS. Windows is not currently validated.
From a checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

worldforge init
worldforge import examples/payments/openapi.yaml
worldforge world inspect
worldforge scenario generate --count 5 --seed 42
worldforge run
```

Each scenario asks the agent to refund a duplicate payment while preserving the original payment.
Run output includes `RUN WF-... PASS` or `RUN WF-... FAIL`, tool-call counts, retries, and steps. A failed scenario
makes `worldforge run` exit with a nonzero status.

Replay any run ID printed by the command:

```bash
worldforge replay WF-0123456789abcdef0123456789abcdef
```

Replace the sample ID with your actual ID. Replay reexecutes recorded simulator actions and
checks their results, state, events, and evaluation. It does not call the original agent.

To test a response lost after a refund was applied, use a separate workspace:

```bash
worldforge init --workspace .worldforge/response-lost
worldforge import examples/payments/openapi.yaml --workspace .worldforge/response-lost
worldforge scenario generate --count 5 --seed 42 --response-lost --workspace .worldforge/response-lost
worldforge run --workspace .worldforge/response-lost
```

Default generated scenarios have no injected failures. Authored `Scenario` objects can also
inject an `error_before` failure before an action mutates state. Failures occur at specified
action occurrences; they do not depend on wall-clock timing.

## What v0.1.0 supports

- Versioned Pydantic contracts for worlds, resources, actions, state, scenarios, events, runs,
  evaluation, and resource usage, with exportable JSON schemas.
- OpenAPI compilation with explicit declared/inferred provenance and compatibility issues.
  [The supported subset](docs/openapi-subset.md) explains which operations execute.
- Per-run synthetic customer, payment, and refund state; integer amounts in minor currency units;
  refund constraints and idempotent refund behavior.
- Seeded scenario generation, `error_before` and `response_lost` failures, bounded agent steps,
  asynchronous adapter hooks, and agent timeouts.
- Explicit objective and constraint evaluation; measured tool calls, retries, failed calls,
  simulation steps, and elapsed time. Unreported tokens and costs remain unavailable.
- Ordered event histories and self-contained local JSON run artifacts with deterministic replay.

OpenAPI describes an interface, not all business behavior. WorldForge's supported
`x-worldforge` annotations declare simulation semantics; limited inference is recorded as
inference. Unsupported operations remain visible with compatibility issues.

## Bring an agent

The default `reference` adapter uses deterministic local logic. The included Python adapter
shows the factory contract:

```bash
worldforge run --agent examples/agents/reference.py --trust-agent-code --agent-timeout 10
```

A custom file exports `create_agent()` returning an implementation of
`worldforge.agents.AgentAdapter`. See [getting started](docs/getting-started.md) for its lifecycle
and run artifacts. Custom adapters are executable Python and require `--trust-agent-code`;
they run with your process's permissions. Timeouts and step limits do not provide isolation
against hostile or blocking Python code.

## Reproducibility and safety

A seed reproduces scenario generation; the simulator and replay use deterministic transitions.
Events have logical timestamps, while run creation time and elapsed duration are measured
wall-clock metadata. External model inference can vary: replay verifies recorded tool actions,
not fresh model responses.

Simulation actions do not execute production API requests. The compiler accepts local references
only and uses safe parsing. Trusted custom adapters can independently access your network and
filesystem, so use external isolation for untrusted code. Local runs can contain sensitive agent
observations and tool data; automatic PII or secret scrubbing is not implemented. Review artifacts
before sharing them. Read [SECURITY.md](SECURITY.md) for the boundary and reporting guidance.

WorldForge v0.1.0 is an experimental foundation. It does not claim production fidelity or sandbox
security. MCP, a dashboard, provider integrations, Failure Oracle, Reality Distillation, and EVOLVE
are future work described in [ROADMAP.md](ROADMAP.md).

## Develop and contribute

```bash
ruff check .
ruff format --check .
mypy src/worldforge
pytest
python -m build
```

CI runs these checks on Python 3.11, 3.12, and 3.13 and exercises the built wheel in a fresh
virtual environment. [CONTRIBUTING.md](CONTRIBUTING.md) covers development branches and review;
[ARCHITECTURE.md](ARCHITECTURE.md) records design decisions; [release instructions](docs/releasing.md)
describe the release checks.

WorldForge is licensed under [Apache-2.0](LICENSE), including its explicit patent grant.

