# Getting started

WorldForge v0.1.0 is a local Python package and CLI. Use Python 3.11+ on Linux or macOS;
Windows is not currently validated. No database, server, model account, or API key is required
for the included payments example and reference agent.

## Install from a checkout

```bash
git clone https://github.com/armanamarnayak-ai/WorldForge.git
cd WorldForge
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

The v0.1 implementation is available on `main`. Cloud tasks should use their existing checkout.

## Compile and inspect a world

```bash
worldforge init
worldforge validate examples/payments/openapi.yaml
worldforge import examples/payments/openapi.yaml
worldforge world inspect
```

`init` creates the local workspace. `validate` checks the specification and reports compatibility
issues without writing a world; unsupported operations yield a nonzero exit status. `import`
compiles and saves `world.json`. Inspect the declared and inferred semantics before trusting a
world's behavior. See [the OpenAPI subset](openapi-subset.md) for accepted annotations and limits.

## Generate, run, and replay

```bash
worldforge scenario generate --count 5 --seed 42
worldforge run
```

Scenarios start from independent synthetic customer and payment state. The task is to refund
the duplicate payment, preserve the original, and obey the refund constraints. The built-in
reference agent inspects the tools and visible observations; it has no provider dependency.
Default scenarios have no injected failures. For a response lost after a refund mutation,
generate into a new workspace using `--response-lost` as shown in the README. The library also
supports authored `Scenario.failures` with an `error_before` failure before mutation.

Run output prints one line per scenario containing its run ID, PASS/FAIL, calls, retries, and
steps. A failed evaluation or unsuccessful termination produces a nonzero exit code. To run
only one saved scenario, pass `--scenario-id` with its ID from `scenarios.json`.

Copy a run ID from the output:

```bash
worldforge replay WF-0123456789abcdef0123456789abcdef
```

Use the actual ID instead of this example. Replay reexecutes recorded actions from the initial
state, checks results and final state, recomputes evaluation, and checks event structure. It
does not rerun external inference. A replay mismatch returns a nonzero exit status.

## Workspace files

Commands that read or write workspace artifacts accept `--workspace PATH` (default `.worldforge`).
`validate` reads only its specification, and `version` prints package metadata. For example:

```bash
worldforge import examples/payments/openapi.yaml --workspace /tmp/worldforge-demo
worldforge scenario generate --count 2 --seed 42 --workspace /tmp/worldforge-demo
worldforge run --workspace /tmp/worldforge-demo
```

Artifacts are local JSON: `world.json`, `scenarios.json`, and `runs/WF-<id>.json`. A run embeds
its world, scenario and initial state, events, final state, and evaluation. It remains self-contained
if the current workspace's world or scenarios change. `import` and `scenario generate` require
`--force` to replace existing files; run logs receive unique IDs.

Event timestamps use a logical clock. Run creation time and elapsed duration are wall-clock
measurements and can differ between equivalent executions. Tool counts and retries are measured.
Token and cost measurements are not collected in v0.1 and remain unavailable.

## Python agent adapters

```bash
worldforge run --agent examples/agents/reference.py --trust-agent-code --agent-timeout 10
```

A custom Python file exports a zero-argument `create_agent()` factory. It returns an
`AgentAdapter` from `worldforge.agents`, with a `metadata` property and async `reset`, `act`, and
`shutdown` hooks. `act(observation)` receives the task, visible data, allowed tool contracts,
the previous tool result, and remaining steps. `act` returns an `AgentDecision` containing an `AgentAction` or a
final message. Consult the typed protocol and [reference factory](../examples/agents/reference.py)
for exact signatures.

The runtime limits steps and awaits hooks with the configured timeout. Custom adapters execute
as trusted Python in the current process; these limits do not sandbox code or reliably interrupt
blocking calls. Replay uses recorded tool actions, so external model outputs need not be
deterministic for a stored run to be replayed.

Do not place credentials or private data in example inputs. Local artifacts can contain sensitive
observations, and automatic scrubbing is unavailable. See [SECURITY.md](../SECURITY.md).

## Check the installation

```bash
ruff check .
ruff format --check .
mypy src/worldforge
pytest
python -m build
```

See [release checks](releasing.md) for installing and exercising the built wheel in a fresh
virtual environment. A synthetic simulation makes no claim of production service fidelity.
