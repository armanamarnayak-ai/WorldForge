"""Local developer workflow; no command contacts production APIs."""

from __future__ import annotations

import importlib.util
import inspect
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, TypeVar, cast

import typer
from pydantic import BaseModel, TypeAdapter

from worldforge import __version__
from worldforge.agents import AgentAdapter, ReferenceAgent
from worldforge.compiler import compile_openapi
from worldforge.models import Scenario, WorldDefinition
from worldforge.runtime import replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios
from worldforge.storage import JsonRunStore

app = typer.Typer(help="Stateful synthetic worlds for reproducible agent evaluation.")
world_app = typer.Typer(help="Inspect the compiled world.")
scenario_app = typer.Typer(help="Generate deterministic payments scenarios.")
app.add_typer(world_app, name="world")
app.add_typer(scenario_app, name="scenario")
T = TypeVar("T", bound=BaseModel)
MAX_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_WORKSPACE = Path(".worldforge")


@contextmanager
def user_errors() -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError, RecursionError) as error:
        typer.echo(f"WorldForge: {error}", err=True)
        raise typer.Exit(2) from error


def read_text(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path}: input must be a regular file")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{path}: input exceeds the 8 MiB limit")
    return data.decode("utf-8")


def load_model(path: Path, model: type[T]) -> T:
    if not path.is_file():
        raise ValueError(f"{path} does not exist. Import a world first.")
    return model.model_validate_json(read_text(path))


def write_artifact(path: Path, text: str, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise ValueError(f"{path} already exists; use --force to replace it") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def compatibility(world: WorldDefinition) -> None:
    supported = sum(action.supported for action in world.actions.values())
    typer.echo(f"World: {world.world_id} v{world.version}; resources={len(world.resources)}")
    typer.echo(f"SUPPORTED {supported}; UNSUPPORTED {len(world.actions) - supported}")
    for issue in world.issues:
        typer.echo(f"{issue.severity.upper()} {issue.location}: {issue.reason}")


@app.command()
def version() -> None:
    """Print the installed package version."""
    typer.echo(__version__)


@app.command()
def init(workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE) -> None:
    """Create local storage without modifying existing artifacts."""
    with user_errors():
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "runs").mkdir(exist_ok=True)
        typer.echo(f"Initialized {workspace}")


@app.command("import")
def import_world(
    spec: Annotated[Path, typer.Argument(help="Local OpenAPI JSON or YAML file.")],
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
    force: Annotated[bool, typer.Option(help="Replace the compiled world artifact.")] = False,
) -> None:
    """Compile a supported OpenAPI subset into a versioned World IR."""
    with user_errors():
        world = compile_openapi(spec)
        write_artifact(
            workspace / "world.json", world.model_dump_json(by_alias=True, indent=2), force
        )
        compatibility(world)
        typer.echo(f"Saved {workspace / 'world.json'}")


@app.command()
def validate(spec: Annotated[Path, typer.Argument()]) -> None:
    """Report compatibility without writing a world artifact."""
    with user_errors():
        world = compile_openapi(spec)
        compatibility(world)
        if any(not action.supported for action in world.actions.values()):
            raise typer.Exit(1)


@world_app.command("inspect")
def inspect_world(workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE) -> None:
    """Print the complete world, schemas, provenance, and compatibility issues."""
    with user_errors():
        world = load_model(workspace / "world.json", WorldDefinition)
        typer.echo(world.model_dump_json(by_alias=True, indent=2))


@scenario_app.command("generate")
def generate(
    count: Annotated[int, typer.Option(min=1, max=1000)] = 5,
    seed: Annotated[int, typer.Option()] = 42,
    response_lost: Annotated[
        bool, typer.Option(help="Lose the first successful refund response.")
    ] = False,
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
    force: Annotated[bool, typer.Option(help="Replace generated scenarios.")] = False,
) -> None:
    """Generate seeded duplicate-charge tasks for the annotated payments world."""
    with user_errors():
        world = load_model(workspace / "world.json", WorldDefinition)
        scenarios = generate_scenarios(world, count=count, seed=seed, response_lost=response_lost)
        serialized = TypeAdapter(list[Scenario]).dump_json(scenarios, by_alias=True, indent=2)
        write_artifact(workspace / "scenarios.json", serialized.decode(), force)
        typer.echo(
            f"Generated {len(scenarios)} scenarios; seed={seed}; response_lost={response_lost}"
        )


def load_agent(selector: str, trust_agent_code: bool) -> AgentAdapter:
    if selector == "reference":
        return ReferenceAgent()
    if not trust_agent_code:
        raise ValueError("Python adapters execute trusted code; opt in with --trust-agent-code")
    path = Path(selector).resolve(strict=True)
    if path.suffix != ".py" or not path.is_file():
        raise ValueError("the agent adapter must be a local .py file")
    spec = importlib.util.spec_from_file_location("worldforge_user_agent", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import agent adapter {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, "create_agent", None)
    if not callable(factory):
        raise ValueError("agent adapter must export create_agent()")
    agent = cast(Callable[[], AgentAdapter], factory)()
    if not all(
        inspect.iscoroutinefunction(getattr(agent, method, None))
        for method in ("reset", "act", "shutdown")
    ):
        raise ValueError("agent reset/act/shutdown must be async methods")
    return agent


@app.command("run")
def run_agent(
    agent: Annotated[
        str, typer.Option(help="reference or a trusted Python adapter path.")
    ] = "reference",
    trust_agent_code: Annotated[bool, typer.Option()] = False,
    scenario_id: Annotated[str | None, typer.Option()] = None,
    agent_timeout: Annotated[float, typer.Option(min=0.001, max=3600)] = 10.0,
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
) -> None:
    """Run an agent through scenarios; persist real results and exit 1 if any fail."""
    with user_errors():
        world = load_model(workspace / "world.json", WorldDefinition)
        path = workspace / "scenarios.json"
        if not path.is_file():
            raise ValueError(f"{path} does not exist. Generate scenarios first.")
        scenarios = TypeAdapter(list[Scenario]).validate_json(read_text(path))
        if scenario_id is not None:
            scenarios = [item for item in scenarios if item.scenario_id == scenario_id]
        if not scenarios:
            raise ValueError("no matching scenarios to run")
        store = JsonRunStore(workspace / "runs")
        passed = 0
        for scenario in scenarios:
            result = run_scenario_sync(
                world, scenario, load_agent(agent, trust_agent_code), agent_timeout=agent_timeout
            )
            store.save(result)
            usage = result.evaluation.usage
            status = "PASS" if result.evaluation.success else "FAIL"
            passed += result.evaluation.success
            typer.echo(
                f"RUN {result.run_id} {status} calls={usage.tool_calls} "
                f"retries={usage.retries} steps={usage.world_steps}"
            )
            for violation in result.evaluation.constraint_violations:
                typer.echo(f"  CONSTRAINT: {violation}")
        typer.echo(
            f"Results: {passed}/{len(scenarios)} passed; tokens=unavailable; cost=unavailable"
        )
        if passed != len(scenarios):
            raise typer.Exit(1)


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
) -> None:
    """Re-execute recorded actions and verify the stored deterministic trajectory."""
    with user_errors():
        run = JsonRunStore(workspace / "runs").load(run_id)
        if not replay_run(run):
            raise ValueError("replay verification failed")
        typer.echo(f"Replay verified: {run_id}; recorded agent outputs were not re-generated")


if __name__ == "__main__":
    app()
