"""Local developer workflow; no command contacts production APIs."""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar, cast

import typer
from pydantic import BaseModel, TypeAdapter

from worldforge import __version__
from worldforge.agents import AgentAdapter, ReferenceAgent, VulnerableRefundAgent
from worldforge.certificates import CertificateStore
from worldforge.compiler import compile_openapi
from worldforge.composition import compose_scenario, compose_worlds, service_world
from worldforge.mcp import compile_mcp
from worldforge.models import (
    Origin,
    Provenance,
    Relationship,
    ResourceReport,
    Scenario,
    SearchSpec,
    WorldDefinition,
)
from worldforge.oracle import (
    criterion_matches,
    default_search_spec,
    falsify_sync,
    reproduce_certificate_sync,
)
from worldforge.providers import MockProvider, ProviderAgentAdapter
from worldforge.resources import aggregate_reports
from worldforge.runtime import replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios
from worldforge.storage import JsonRunStore

app = typer.Typer(help="Stateful synthetic worlds for reproducible agent evaluation.")
world_app = typer.Typer(help="Inspect the compiled world.")
scenario_app = typer.Typer(help="Generate deterministic payments scenarios.")
failure_app = typer.Typer(help="Inspect and reproduce saved Failure Certificates.")
resources_app = typer.Typer(help="Inspect measured run resources and provenance.")
app.add_typer(world_app, name="world")
app.add_typer(scenario_app, name="scenario")
app.add_typer(failure_app, name="failure")
app.add_typer(resources_app, name="resources")
T = TypeVar("T", bound=BaseModel)
MAX_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_WORKSPACE = Path(".worldforge")


class InputFormat(StrEnum):
    openapi = "openapi"
    mcp = "mcp"


def compile_input(path: Path, input_format: InputFormat) -> WorldDefinition:
    return compile_mcp(path) if input_format == InputFormat.mcp else compile_openapi(path)


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
    if len((text + "\n").encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("artifact exceeds the 8 MiB limit")
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
    spec: Annotated[Path, typer.Argument(help="Local OpenAPI or static MCP JSON/YAML file.")],
    input_format: Annotated[InputFormat, typer.Option("--format")] = InputFormat.openapi,
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
    force: Annotated[bool, typer.Option(help="Replace the compiled world artifact.")] = False,
) -> None:
    """Compile declared local definitions into a versioned World IR."""
    with user_errors():
        world = compile_input(spec, input_format)
        write_artifact(
            workspace / "world.json", world.model_dump_json(by_alias=True, indent=2), force
        )
        compatibility(world)
        typer.echo(f"Saved {workspace / 'world.json'}")


@app.command()
def validate(
    spec: Annotated[Path, typer.Argument()],
    input_format: Annotated[InputFormat, typer.Option("--format")] = InputFormat.openapi,
) -> None:
    """Report compatibility without writing a world artifact."""
    with user_errors():
        world = compile_input(spec, input_format)
        compatibility(world)
        if any(not action.supported for action in world.actions.values()):
            raise typer.Exit(1)


@world_app.command("inspect")
def inspect_world(workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE) -> None:
    """Print the complete world, schemas, provenance, and compatibility issues."""
    with user_errors():
        world = load_model(workspace / "world.json", WorldDefinition)
        typer.echo(world.model_dump_json(by_alias=True, indent=2))


@world_app.command("compose")
def compose(
    service: Annotated[
        list[str], typer.Option(help="namespace=local-world.json; repeat per service.")
    ],
    link: Annotated[
        list[str] | None, typer.Option(help="namespace.resource.field=namespace.resource.")
    ] = None,
    world_id: Annotated[str, typer.Option()] = "composed",
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
    force: Annotated[bool, typer.Option()] = False,
) -> None:
    """Compose explicit local service contracts and relationships."""
    with user_errors():
        services: dict[str, WorldDefinition] = {}
        if len(service) > 8:
            raise ValueError("composition is limited to eight services")
        for item in service:
            namespace, separator, filename = item.partition("=")
            if not separator or not filename or namespace in services:
                raise ValueError("each service must be a unique namespace=local-world.json")
            services[namespace] = load_model(Path(filename), WorldDefinition)
        relationships: list[Relationship] = []
        if len(link or []) > 64:
            raise ValueError("composition is limited to 64 explicit cross-service links")
        for item in link or []:
            source, separator, target = item.partition("=")
            resource, dot, field = source.rpartition(".")
            if not separator or not dot or not field or not target:
                raise ValueError("link must be namespace.resource.field=namespace.resource")
            relationships.append(
                Relationship(
                    resource=resource,
                    field=field,
                    target_resource=target,
                    provenance=Provenance(
                        origin=Origin.DECLARED, source="cli:--link", reason=f"Explicit link {item}"
                    ),
                )
            )
        world = compose_worlds(services, world_id=world_id, relationships=relationships)
        write_artifact(
            workspace / "world.json", world.model_dump_json(by_alias=True, indent=2), force
        )
        compatibility(world)
        typer.echo(f"Saved {workspace / 'world.json'}")


@scenario_app.command("generate")
def generate(
    count: Annotated[int, typer.Option(min=1, max=1000)] = 5,
    seed: Annotated[int, typer.Option()] = 42,
    response_lost: Annotated[
        bool, typer.Option(help="Lose the first successful refund response.")
    ] = False,
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
    namespace: Annotated[
        str | None, typer.Option(help="Payments service in a composed world.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace generated scenarios.")] = False,
) -> None:
    """Generate seeded duplicate-charge tasks for the annotated payments world."""
    with user_errors():
        world = load_model(workspace / "world.json", WorldDefinition)
        source = service_world(world, namespace) if namespace is not None else world
        scenarios = generate_scenarios(source, count=count, seed=seed, response_lost=response_lost)
        if namespace is not None:
            scenarios = [compose_scenario(item, world, namespace) for item in scenarios]
        serialized = TypeAdapter(list[Scenario]).dump_json(scenarios, by_alias=True, indent=2)
        write_artifact(workspace / "scenarios.json", serialized.decode(), force)
        typer.echo(
            f"Generated {len(scenarios)} scenarios; seed={seed}; response_lost={response_lost}"
        )


def load_agent(selector: str, trust_agent_code: bool) -> AgentAdapter:
    if selector == "reference":
        return ReferenceAgent()
    if selector == "vulnerable-reference":
        return VulnerableRefundAgent()
    if selector == "mock-provider":
        return ProviderAgentAdapter(MockProvider())
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
        if len(scenarios) > 1000:
            raise ValueError("a batch is limited to 1000 scenarios")
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
            f"Results: {passed}/{len(scenarios)} passed; inspect resources for measured usage"
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


@app.command("falsify")
def falsify_agent(
    agent: Annotated[
        str, typer.Option(help="reference, vulnerable-reference, mock-provider, or trusted .py.")
    ] = "vulnerable-reference",
    search: Annotated[
        Path | None, typer.Option(help="Bounded SearchSpec JSON; defaults to the refund adversary.")
    ] = None,
    trust_agent_code: Annotated[bool, typer.Option()] = False,
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
) -> None:
    """Search, minimize, and repeatedly verify a configured simulation failure."""
    with user_errors():
        world = load_model(workspace / "world.json", WorldDefinition)
        spec = load_model(search, SearchSpec) if search is not None else default_search_spec(world)
        # Validate the selector before search; persisted paths never grant execution permission.
        if (
            agent not in {"reference", "vulnerable-reference", "mock-provider"}
            and not trust_agent_code
        ):
            raise ValueError("Python adapters execute trusted code; opt in with --trust-agent-code")
        outcome = falsify_sync(
            world, spec, lambda: load_agent(agent, trust_agent_code), {"selector": agent}
        )
        run_store = JsonRunStore(workspace / "runs")
        for run in outcome.runs:
            run_store.save(run)
        audit = outcome.audit
        typer.echo(
            f"Search {outcome.status}; candidates={audit.candidate_runs}; "
            f"minimization={audit.minimization_runs}; verification={audit.verification_runs}; "
            f"total_runs={audit.total_runs}; budget_exhausted={audit.budget_exhausted}"
        )
        if outcome.certificate is not None:
            certificate = outcome.certificate
            path = CertificateStore(workspace / "failures").save(certificate)
            typer.echo(
                f"CERTIFICATE {certificate.certificate_id}; "
                f"classification={certificate.classification}; "
                f"conditions={len(certificate.original_conditions)}"
                f"->{len(certificate.minimized_conditions)}; "
                f"reproduced={certificate.successful_reproduction_count}"
                f"/{certificate.reproduction_count}; "
                f"one_minimal={audit.one_minimal}"
            )
            typer.echo(f"Saved {path}")
        else:
            # Retain an inspectable audit even when no verified certificate was found.
            filename = (
                f"search-{outcome.runs[0].run_id}.json" if outcome.runs else "search-empty.json"
            )
            write_artifact(
                workspace / "searches" / filename,
                json.dumps(
                    {
                        "status": outcome.status,
                        "spec": spec.model_dump(mode="json"),
                        "audit": audit.model_dump(mode="json"),
                        "events": [event.model_dump(mode="json") for event in outcome.events],
                        "run_ids": [run.run_id for run in outcome.runs],
                    },
                    indent=2,
                ),
                False,
            )
            typer.echo("No verified certificate; this bounded search is not proof of safety.")
            raise typer.Exit(1)


@failure_app.command("inspect")
def inspect_failure(
    certificate_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
) -> None:
    """Validate and print certificate evidence without executing its agent."""
    with user_errors():
        certificate = CertificateStore(workspace / "failures").load(certificate_id)
        typer.echo(certificate.model_dump_json(by_alias=True, indent=2))


@failure_app.command("reproduce")
def reproduce_failure(
    certificate_id: Annotated[str, typer.Argument()],
    agent: Annotated[
        str | None, typer.Option(help="Override the recorded built-in agent selector.")
    ] = None,
    count: Annotated[int, typer.Option(min=1, max=20)] = 3,
    agent_timeout: Annotated[float | None, typer.Option(min=0.01, max=60)] = None,
    trust_agent_code: Annotated[bool, typer.Option()] = False,
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
) -> None:
    """Execute a bounded reproduction; stored Python paths require explicit selection."""
    with user_errors():
        certificate = CertificateStore(workspace / "failures").load(certificate_id)
        adapter = certificate.agent_configuration.get("adapter")
        stored_selector = adapter.get("selector") if isinstance(adapter, dict) else None
        selector = agent or stored_selector
        if not isinstance(selector, str):
            raise ValueError("select an agent explicitly with --agent")
        if agent is None and selector not in {"reference", "vulnerable-reference", "mock-provider"}:
            raise ValueError(
                "stored Python paths are not executed automatically; "
                "select --agent and --trust-agent-code"
            )
        timeout = agent_timeout
        if timeout is None:
            stored_timeout = certificate.agent_configuration.get("agent_timeout", 2.0)
            if (
                isinstance(stored_timeout, bool)
                or not isinstance(stored_timeout, int | float)
                or not 0 < stored_timeout <= 60
            ):
                raise ValueError(
                    "invalid recorded agent timeout; select --agent-timeout explicitly"
                )
            timeout = float(stored_timeout)
        runs = reproduce_certificate_sync(
            certificate, lambda: load_agent(selector, trust_agent_code), count, timeout
        )
        store = JsonRunStore(workspace / "runs")
        successes = 0
        for run in runs:
            store.save(run)
            matches = criterion_matches(run, certificate.criterion)
            successes += matches
            typer.echo(f"RUN {run.run_id} failure_reproduced={matches}")
        typer.echo(f"Failure reproduced: {successes}/{len(runs)}; agent={selector}")
        if successes != len(runs):
            raise typer.Exit(1)


def require_report(report: ResourceReport | None) -> ResourceReport:
    if report is None:
        raise ValueError(
            "legacy run has no v0.2 resource report; existing evaluation counters remain available"
        )
    return report


@resources_app.command("report")
def report_resources(
    run_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
) -> None:
    """Print measurements, provenance, warnings, and typed provider metadata."""
    with user_errors():
        run = JsonRunStore(workspace / "runs").load(run_id)
        typer.echo(require_report(run.resource_report).model_dump_json(indent=2))


@resources_app.command("aggregate")
def aggregate_resources(
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 1000,
    workspace: Annotated[Path, typer.Option()] = DEFAULT_WORKSPACE,
) -> None:
    """Sum a bounded selection of saved reports; unavailable values remain unavailable."""
    with user_errors():
        directory = workspace / "runs"
        if not directory.is_dir():
            raise ValueError("no saved runs")
        store = JsonRunStore(directory)
        paths: list[Path] = []
        for path in directory.glob("WF-*.json"):
            paths.append(path)
            if len(paths) > limit:
                raise ValueError(f"saved runs are exceeding limit {limit}; use a smaller workspace")
        paths.sort()
        if not paths:
            raise ValueError("no saved runs")
        reports = [require_report(store.load(path.stem).resource_report) for path in paths]
        typer.echo(aggregate_reports(reports).model_dump_json(indent=2))


if __name__ == "__main__":
    app()
