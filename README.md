# WorldForge — v0.2 development

**Give your AI a world to learn in before giving it yours.**

WorldForge is an open-source artificial experience engine for stateful synthetic environments
where autonomous AI agents can be evaluated, stressed, observed, and replayed without executing
production APIs.

WorldForge v0.1.0 is the released foundation. This branch develops **v0.2 — Integration and
Failure Discovery**, with package version `0.2.0.dev0`. It extends the existing local Python
library and CLI; it is not a published v0.2 release. The `v0.1.0` tag remains immutable.

```text
local OpenAPI / static MCP tools → typed world → bounded failure scenarios → agent
                                                                          ↓
                                               evaluation + resource reporting
                                                                          ↓
                         Failure Oracle → minimized Failure Certificate → reproduce / replay
```

The included reference, vulnerable demonstration, and mock-provider adapters need no account,
API key, network connection to an AI provider, or paid service.

## Try the payments example

Requires Python 3.11 or newer on Linux or macOS. Windows is not currently validated.
From a checkout of this development branch:

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

Each scenario asks the agent to refund a duplicate payment while preserving the original.
Run output includes `RUN WF-... PASS` or `RUN WF-... FAIL`, calls, retries, and steps.
Failed evaluations produce a nonzero exit status. Copy an actual run ID from the output:

```bash
worldforge replay WF-0123456789abcdef0123456789abcdef
worldforge resources report WF-0123456789abcdef0123456789abcdef
worldforge resources aggregate
```

Replay reexecutes recorded simulator actions and verifies results, state, events, evaluation,
and deterministic resource measurements. It does not call the original agent or regenerate
external model inference. Actual elapsed time is recorded wall-clock metadata.

## Discover and reproduce an unsafe retry

Use a fresh workspace so this search does not replace your existing example artifacts:

```bash
worldforge init --workspace .worldforge/oracle
worldforge import examples/payments/openapi.yaml --workspace .worldforge/oracle
worldforge falsify --agent vulnerable-reference --search examples/oracle/refund-search.json --workspace .worldforge/oracle
```

The search first tests an ordinary scenario, then a bounded pool containing a simulated delay
and a response lost after a successful refund. The deliberately vulnerable agent changes its
idempotency key on retry. WorldForge detects `unsafe_retry_key_changed`, attempts to remove
irrelevant conditions, and verifies the retained failure repeatedly before issuing a certificate.

The payments ledger still enforces conservation and idempotency: the unsafe second full refund
is rejected with `409`. The demonstration discovers an **unsafe retry attempt**, not an actual
double refund. The safe reference agent reuses its original key and does not exhibit this failure.

Use the actual `WF-F-...` certificate ID printed by `falsify`:

```bash
worldforge failure inspect WF-F-0123456789abcdef0123456789abcdef --workspace .worldforge/oracle
worldforge failure reproduce WF-F-0123456789abcdef0123456789abcdef --workspace .worldforge/oracle

worldforge import examples/payments/openapi.yaml --workspace .worldforge/oracle-safe
worldforge falsify --agent reference --search examples/oracle/refund-search.json --workspace .worldforge/oracle-safe
```

A search that finds no qualifying failure exits nonzero; it is evidence about the tested finite
space, not proof that the agent is universally safe. See [the Failure Oracle guide](docs/v0.2-failure-oracle.md)
for criteria, budgets, minimization, and certificate interpretation.

## Available in this development branch

- The v0.1 OpenAPI compiler, payments world, seeded scenarios, `AgentAdapter`, event histories,
  explicit evaluation, local run storage, CLI, and recorded-action replay.
- Local/static MCP tool compilation into the same typed World IR. Tool names, descriptions,
  parameter schemas, optional output schemas, provenance, and compatibility issues are retained.
  Executable behavior requires explicit WorldForge annotations; no live MCP server is contacted.
- Ten deterministic failure kinds, including virtual delay, pre-effect timeout, lost response,
  duplicate delivery, stale reads, denial, rate limiting, unavailable service, and observable
  response corruption. Effects remain bounded and recorded.
- A bounded payments-family Failure Oracle with deletion minimization, repeated verification,
  structured Failure Certificates, inspect/reproduce commands, and associated replayable runs.
- Resource reports and aggregation with `EXACT`, `CALCULATED`, `ESTIMATED`, or `UNAVAILABLE`
  provenance. Provider tokens, quotas, and costs stay unavailable unless explicitly reported or
  configured; no guessed usage is presented as measurement.
- A provider-neutral model boundary and deterministic local mock implementation. No real-provider
  SDK, credential discovery, or paid API call is required by the core workflow.
- Explicit composition of namespaced service worlds, shared simulation/event boundaries, and
  declared cross-service relationships, proven with a small synthetic email fixture.

For static MCP input, use:

```bash
worldforge import examples/mcp/payments.json --format mcp --workspace .worldforge/mcp
worldforge scenario generate --count 2 --seed 42 --workspace .worldforge/mcp
worldforge run --workspace .worldforge/mcp
```

Read the [MCP subset](docs/mcp-subset.md), [failure model](docs/v0.2-failure-model.md),
[resource/provider guide](docs/v0.2-resources-providers.md), and
[composition guide](docs/v0.2-composition.md). [Getting started](docs/getting-started.md)
documents the basic workflow.

## Bring an agent

`reference` is a narrow deterministic payments policy. `vulnerable-reference` is an intentionally
unsafe demonstration policy; `mock-provider` exercises the provider boundary locally.
A custom file exports `create_agent()` returning an implementation of
`worldforge.agents.AgentAdapter`:

```bash
worldforge run --agent examples/agents/reference.py --trust-agent-code --agent-timeout 10
```

Custom adapters are executable trusted Python and require `--trust-agent-code`. They run with
the current process's permissions. Step limits and asynchronous timeouts do not isolate hostile
code or reliably interrupt blocking Python calls.

## Compatibility and security

Existing v0.1 World IR and scenario artifacts remain readable. Richer scenarios and composed
worlds use schema `1.1`; new runs use `1.1`. The released `1.0` JSON schemas remain frozen, and
legacy runs retain their original replay/evaluation semantics. Failure Certificates, search
specifications, and resource reports have independent `1.0` contracts. See
[the architecture and migration notes](docs/v0.2-architecture.md).

Compilation and simulation use local, bounded inputs and offline schemas; they do not execute
OpenAPI server URLs or MCP tools on real servers. Descriptions and synthetic observations remain
untrusted data. Trusted adapters can independently access the network, filesystem, and secrets.
Artifacts may contain sensitive observations; automatic PII/secret scrubbing and true sandboxing
are not implemented. See [SECURITY.md](SECURITY.md) and [v0.2 security/replay notes](docs/v0.2-security-replay.md).

## Future work

A dashboard is deferred. Live MCP connections, a larger service catalog, and optional real-provider
integrations remain future work. Reality Distillation, telemetry/log/HAR ingestion, fidelity
calibration, Capability Genome, EVOLVE, adaptive curricula, training/RL pipelines, autonomous
self-modification, production API execution, and cloud SaaS are not implemented in v0.2.
[ROADMAP.md](ROADMAP.md) records future directions, not shipped capabilities.

## Develop and contribute

```bash
ruff check .
ruff format --check .
mypy src/worldforge
pytest
python -m build
```

CI is configured for Python 3.11, 3.12, and 3.13 and exercises the built wheel outside the checkout.
Configured jobs are not evidence that a particular commit passed; validation evidence belongs
in the review record and PR. [CONTRIBUTING.md](CONTRIBUTING.md) covers development branches and
review; [ARCHITECTURE.md](ARCHITECTURE.md) records design decisions. The historical
[v0.1 review](docs/v0.1-review.md) is preserved; [the v0.2 review](docs/v0.2-review.md) tracks this work.

WorldForge is licensed under [Apache-2.0](LICENSE), including its explicit patent grant.
