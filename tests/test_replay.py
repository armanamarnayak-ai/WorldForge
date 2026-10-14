from __future__ import annotations

from pathlib import Path

import pytest

from worldforge.agents import ReferenceAgent
from worldforge.compiler import compile_openapi
from worldforge.models import Run
from worldforge.runtime import ReplayError, _timestamp, replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios


def make_run() -> Run:
    world = compile_openapi(Path(__file__).parents[1] / "examples/payments/openapi.yaml")
    scenario = generate_scenarios(world, count=1, seed=5, response_lost=True)[0]
    return run_scenario_sync(world, scenario, ReferenceAgent())


@pytest.mark.parametrize(
    "event_type",
    [
        "OBSERVATION",
        "AGENT_DECISION",
        "TOOL_CALLED",
        "TOOL_RETURNED",
        "FAILURE_INJECTED",
        "WORLD_STATE_CHANGED",
        "AGENT_RESET",
        "AGENT_SHUTDOWN",
        "RUN_COMPLETED",
    ],
)
def test_missing_events_rejected_even_with_repaired_sequences(event_type: str) -> None:
    run = make_run()
    index = next(index for index, event in enumerate(run.events) if event.type == event_type)
    del run.events[index]
    for sequence, event in enumerate(run.events):
        event.sequence = sequence
        event.timestamp = _timestamp(sequence)
    with pytest.raises(ReplayError):
        replay_run(run)


@pytest.mark.parametrize(
    "target", ["observation", "action", "result", "transition", "final", "evaluation"]
)
def test_tampered_artifacts_rejected(target: str) -> None:
    run = make_run()
    if target == "observation":
        event = next(event for event in run.events if event.type == "OBSERVATION")
        event.payload = {"observation": {"task": "different"}}
    elif target == "action":
        event = next(event for event in run.events if event.type == "TOOL_CALLED")
        event.payload = {"action": {"tool": "list_payments", "arguments": {}}}
    elif target == "result":
        event = next(event for event in run.events if event.type == "TOOL_RETURNED")
        event.payload = {"result": {"status": 200, "data": None, "error": None}}
    elif target == "transition":
        event = next(event for event in run.events if event.type == "WORLD_STATE_CHANGED")
        event.payload = {"before": {}, "after": {}}
    elif target == "final":
        run.final_state.resources["refund"].clear()
    else:
        run.evaluation.success = False
    with pytest.raises(ReplayError):
        replay_run(run)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "WF-" + "a" * 32),
        ("scenario_id", "changed"),
        ("world_id", "changed"),
        ("world_version", "2"),
        ("timestamp", "now"),
        ("sequence", 99),
    ],
)
def test_wrong_event_context_rejected(field: str, value: object) -> None:
    run = make_run()
    setattr(run.events[3], field, value)
    with pytest.raises(ReplayError):
        replay_run(run)


def test_extra_or_duplicate_event_rejected() -> None:
    run = make_run()
    run.events.append(run.events[-1].model_copy(deep=True))
    run.events[-1].sequence = len(run.events) - 1
    run.events[-1].timestamp = _timestamp(len(run.events) - 1)
    with pytest.raises(ReplayError):
        replay_run(run)


def test_fresh_runs_are_deterministic_except_run_metadata_and_elapsed_time() -> None:
    first, second = make_run(), make_run()
    assert first.run_id != second.run_id
    assert first.scenario == second.scenario
    assert first.final_state == second.final_state
    assert [(event.type, event.timestamp, event.payload) for event in first.events[:-1]] == [
        (event.type, event.timestamp, event.payload) for event in second.events[:-1]
    ]
    first.evaluation.usage.elapsed_seconds = 0
    second.evaluation.usage.elapsed_seconds = 0
    assert first.evaluation == second.evaluation
