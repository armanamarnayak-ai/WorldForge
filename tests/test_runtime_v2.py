from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import worldforge.runtime as runtime
from worldforge.agents import ReferenceAgent, VulnerableRefundAgent
from worldforge.compiler import compile_openapi
from worldforge.models import (
    AgentAction,
    AgentDecision,
    AgentObservation,
    FailureSpec,
    Origin,
    Provenance,
    ProviderIdentity,
    ProviderRecord,
    ProviderUsage,
    WorldAction,
    WorldResource,
)
from worldforge.runtime import evaluate_run, replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios
from worldforge.storage import JsonRunStore

EXAMPLE = Path(__file__).parents[1] / "examples/payments/openapi.yaml"


def test_lost_refund_response_exposes_unsafe_retry_without_weakening_ledger() -> None:
    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42, response_lost=True)[0]
    run = run_scenario_sync(world, scenario, VulnerableRefundAgent())
    assert run.evaluation.objective_completed
    assert not run.evaluation.success
    assert run.evaluation.constraint_violations == ["unsafe_retry_key_changed"]
    assert len(run.final_state.resources["refund"]) == 1
    returned = [event.payload["result"] for event in run.events if event.type == "TOOL_RETURNED"]
    assert [result["status"] for result in returned] == [200, 504, 409]
    assert run.resource_report is not None
    assert run.resource_report.metrics["retries"].value == 1
    assert run.evaluation.usage.retries == 1
    assert replay_run(run)
    safe = run_scenario_sync(world, scenario, ReferenceAgent())
    assert safe.evaluation.success
    assert len(safe.final_state.resources["refund"]) == 1


def test_unsafe_retry_evaluator_is_explicitly_opt_in_for_sdk_compatibility() -> None:
    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42, response_lost=True)[0]
    run = run_scenario_sync(world, scenario, VulnerableRefundAgent())
    actions = [event.payload["action"] for event in run.events if event.type == "TOOL_CALLED"]
    results = [event.payload["result"] for event in run.events if event.type == "TOOL_RETURNED"]
    from worldforge.models import AgentAction, ToolResult

    calls = [
        (AgentAction.model_validate(action), ToolResult.model_validate(result))
        for action, result in zip(actions, results, strict=True)
    ]
    assert evaluate_run(scenario, run.final_state, "agent_finished", calls).success
    assert not evaluate_run(
        scenario, run.final_state, "agent_finished", calls, detect_unsafe_retries=True
    ).success


def test_virtual_latency_and_failure_details_are_measured_and_replayable() -> None:
    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    scenario.schema_version = "1.1"
    scenario.failures = [FailureSpec(action="list_payments", kind="delay", latency_ms=750)]
    run = run_scenario_sync(world, scenario, ReferenceAgent())
    assert run.evaluation.success
    assert run.resource_report is not None
    assert run.resource_report.metrics["simulated_latency_ms"].value == 750
    failure = next(event for event in run.events if event.type == "FAILURE_INJECTED")
    assert failure.payload["details"] == {"kind": "delay", "effect_committed": False}
    assert sum(event.type == "SIMULATED_LATENCY" for event in run.events) == 2
    assert replay_run(run)


def test_total_runtime_budget_bounds_cooperative_callbacks_and_preserves_cleanup() -> None:
    class SlowAgent(ReferenceAgent):
        shutdown_called = False

        async def act(self, observation: AgentObservation) -> AgentDecision:
            await asyncio.sleep(0.5)
            return await super().act(observation)

        async def shutdown(self) -> None:
            self.shutdown_called = True

    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    scenario.schema_version = "1.1"
    scenario.max_runtime_seconds = 0.1
    agent = SlowAgent()
    run = run_scenario_sync(world, scenario, agent, agent_timeout=1)
    assert run.evaluation.termination in (
        "agent_reset_timeout",
        "agent_act_timeout",
        "runtime_limit",
    )
    assert run.evaluation.usage.elapsed_seconds < 0.4
    assert agent.shutdown_called
    assert replay_run(run)


def test_oversized_agent_outputs_fail_before_effect_and_do_not_persist_metadata() -> None:
    class LargeMetadataAgent(ReferenceAgent):
        @property
        def metadata(self):  # type: ignore[no-untyped-def]
            return {"too_large": "x" * 70_000}

    class LargeDecisionAgent(ReferenceAgent):
        async def act(self, observation: AgentObservation) -> AgentDecision:
            return AgentDecision(message="x" * 300_000)

    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    for agent, phase in [(LargeMetadataAgent(), "metadata"), (LargeDecisionAgent(), "act")]:
        run = run_scenario_sync(world, scenario, agent)
        assert run.evaluation.termination == f"agent_{phase}_error"
        assert run.evaluation.usage.tool_calls == 0
        assert "too_large" not in run.agent_metadata
        assert replay_run(run)


class TraceLoopAgent(ReferenceAgent):
    def __init__(self, message: str = "") -> None:
        super().__init__()
        self.message = message

    async def act(self, observation: AgentObservation) -> AgentDecision:
        return AgentDecision(action=AgentAction(tool="list_payments"), message=self.message)


def test_real_trace_limit_bounds_repeated_large_observations_and_persists(tmp_path) -> None:  # type: ignore[no-untyped-def]
    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    scenario.visible["synthetic_large_observation"] = "x" * 900_000
    scenario.max_steps = 100
    run = run_scenario_sync(world, scenario, TraceLoopAgent())
    assert run.evaluation.termination == "artifact_limit"
    assert 2 < run.evaluation.usage.tool_calls < 100
    event = next(event for event in run.events if event.type == "TRACE_BUDGET_EXHAUSTED")
    assert event.payload["phase"] == "observation"
    assert run.final_state.step == run.evaluation.usage.tool_calls
    store = JsonRunStore(tmp_path)
    path = store.save(run)
    assert path.stat().st_size < 32 * 1024 * 1024
    assert replay_run(store.load(run.run_id))


def test_decision_trace_limit_retains_rejected_decision_and_reported_usage(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(runtime, "_MAX_TRACE_BYTES", 1024 * 1024)
    monkeypatch.setattr(runtime, "_TRACE_FINAL_RESERVE_BYTES", 512 * 1024)

    class UsageLoop(TraceLoopAgent):
        def drain_provider_records(self) -> list[ProviderRecord]:
            return [
                ProviderRecord(
                    identity=ProviderIdentity(provider="local", model="fixture"),
                    usage=ProviderUsage(requests=1),
                )
            ]

    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    run = run_scenario_sync(world, scenario, UsageLoop("x" * 150_000))
    assert run.evaluation.termination == "artifact_limit"
    event = next(event for event in run.events if event.type == "TRACE_BUDGET_EXHAUSTED")
    assert event.payload["phase"] == "decision"
    assert event.payload["rejected_events"][-1]["type"] == "AGENT_DECISION"
    assert (
        run.resource_report.metrics["provider_requests"].value
        == run.evaluation.usage.tool_calls + 1
    )
    assert replay_run(run)
    event.payload["pending_bytes"] += 1
    with pytest.raises(runtime.ReplayError):
        replay_run(run)


def blob_example():  # type: ignore[no-untyped-def]
    world = compile_openapi(EXAMPLE)
    provenance = Provenance(origin=Origin.DECLARED, source="test:blob", reason="bounded fixture")
    schema = {
        "type": "object",
        "required": ["id", "payload"],
        "properties": {"id": {"type": "string"}, "payload": {"type": "string"}},
    }
    world.resources["blob"] = WorldResource(name="blob", schema=schema, provenance=provenance)
    world.actions["create_blob"] = WorldAction(
        operation_id="create_blob",
        method="POST",
        path="/blobs",
        kind="create",
        resource="blob",
        input_schema={"type": "object", "properties": {"payload": {"type": "string"}}},
        provenance=provenance,
    )
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    scenario.allowed_actions.append("create_blob")

    class BlobAgent(ReferenceAgent):
        async def act(self, observation: AgentObservation) -> AgentDecision:
            return AgentDecision(
                action=AgentAction(tool="create_blob", arguments={"payload": "x" * 150_000})
            )

    return world, scenario, BlobAgent()


@pytest.mark.parametrize("state_budget", [128 * 1024, 4 * 1024 * 1024])
def test_staged_mutation_stops_at_state_or_tool_trace_budget_without_partial_effect(
    monkeypatch,
    state_budget: int,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(runtime, "_MAX_TRACE_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(runtime, "_TRACE_FINAL_RESERVE_BYTES", 512 * 1024)
    monkeypatch.setattr(runtime, "_MAX_STATE_BYTES", state_budget)
    world, scenario, agent = blob_example()
    run = run_scenario_sync(world, scenario, agent)
    assert run.evaluation.termination == "artifact_limit"
    event = next(event for event in run.events if event.type == "TRACE_BUDGET_EXHAUSTED")
    assert event.payload["phase"] == "tool"
    assert len(run.final_state.resources["blob"]) == run.evaluation.usage.tool_calls
    assert run.final_state.step == run.evaluation.usage.tool_calls
    assert (event.payload["state_bytes"] > state_budget) == (state_budget == 128 * 1024)
    assert replay_run(run)
    path = JsonRunStore(tmp_path).save(run)
    assert path.stat().st_size < runtime._MAX_TRACE_BYTES + 128 * 1024
    event.payload["state_bytes"] += 1
    with pytest.raises(runtime.ReplayError):
        replay_run(run)


def test_oversized_context_is_rejected_before_agent_callbacks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class UntouchedAgent(ReferenceAgent):
        reset_called = False

        async def reset(self) -> None:
            self.reset_called = True

    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    monkeypatch.setattr(runtime, "_MAX_CONTEXT_BYTES", 1024)
    agent = UntouchedAgent()
    with pytest.raises(ValueError, match="context budget"):
        run_scenario_sync(world, scenario, agent)
    assert not agent.reset_called
