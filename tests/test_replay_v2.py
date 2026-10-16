from __future__ import annotations

import json
from pathlib import Path

import pytest

import worldforge.runtime as runtime
from worldforge.compiler import compile_openapi
from worldforge.models import FailureSpec, Run
from worldforge.providers import MockProvider, ProviderAgentAdapter
from worldforge.runtime import ReplayError, _timestamp, replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("fixture", ["v0.1-run.json", "v0.1-unsafe-run.json"])
def test_immutable_v010_artifacts_replay_with_original_wire_contract(fixture: str) -> None:
    data = json.loads((ROOT / "tests/fixtures" / fixture).read_text())
    run = Run.model_validate(data)
    assert run.schema_version == "1.0"
    assert run.evaluation.success
    assert run.model_dump(mode="json", by_alias=True) == data
    assert replay_run(run)


def make_run() -> Run:
    world = compile_openapi(ROOT / "examples/payments/openapi.yaml")
    scenario = generate_scenarios(world, count=1, seed=42, response_lost=True)[0]
    scenario.schema_version = "1.1"
    scenario.failures.append(FailureSpec(action="list_payments", kind="delay", latency_ms=20))
    return run_scenario_sync(world, scenario, ProviderAgentAdapter(MockProvider()))


@pytest.mark.parametrize(
    "event_type", ["PROVIDER_USAGE", "SIMULATED_LATENCY", "RESOURCE_USAGE", "FAILURE_INJECTED"]
)
def test_v11_replay_rejects_removed_observability_events(event_type: str) -> None:
    run = make_run()
    index = next(i for i, event in enumerate(run.events) if event.type == event_type)
    del run.events[index]
    for sequence, event in enumerate(run.events):
        event.sequence = sequence
        event.timestamp = _timestamp(sequence)
    with pytest.raises(ReplayError):
        replay_run(run)


@pytest.mark.parametrize("target", ["report", "provider", "latency", "failure_details"])
def test_v11_replay_rejects_tampered_measurements_and_diagnostics(target: str) -> None:
    run = make_run()
    assert run.resource_report is not None
    if target == "report":
        run.resource_report.metrics["tool_calls"].value = 100
    elif target == "provider":
        event = next(event for event in run.events if event.type == "PROVIDER_USAGE")
        event.payload["record"]["usage"]["requests"] = 4
    elif target == "latency":
        event = next(event for event in run.events if event.type == "SIMULATED_LATENCY")
        event.payload["latency_ms"] = 999
    else:
        event = next(event for event in run.events if event.type == "FAILURE_INJECTED")
        event.payload["details"]["effect_committed"] = True
    with pytest.raises(ReplayError):
        replay_run(run)


def test_wallclock_measurement_is_not_claimed_as_deterministic_simulation() -> None:
    first, second = make_run(), make_run()
    assert first.resource_report is not None
    assert second.resource_report is not None
    assert first.resource_report.metrics["actual_runtime_latency_ms"].value > 0
    assert [(event.type, event.payload) for event in first.events[:-1]] == [
        (event.type, event.payload) for event in second.events[:-1]
    ]
    assert replay_run(first) and replay_run(second)


def test_modern_replay_enforces_same_context_admission_budget_as_runtime(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    run = make_run()
    monkeypatch.setattr(runtime, "_MAX_CONTEXT_BYTES", 1024)
    with pytest.raises(ReplayError, match="context budget"):
        replay_run(run)
