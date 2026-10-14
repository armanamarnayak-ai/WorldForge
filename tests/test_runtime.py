from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from worldforge.agents import ReferenceAgent
from worldforge.compiler import compile_openapi
from worldforge.models import AgentAction, AgentDecision, AgentObservation, FailureInjection
from worldforge.runtime import replay_run, run_scenario, run_scenario_sync
from worldforge.scenarios import generate_scenarios

EXAMPLE = Path(__file__).parents[1] / "examples/payments/openapi.yaml"


def example(response_lost: bool = False):  # type: ignore[no-untyped-def]
    world = compile_openapi(EXAMPLE)
    return world, generate_scenarios(world, count=1, seed=42, response_lost=response_lost)[0]


@pytest.mark.parametrize("response_lost", [False, True])
def test_reference_exact_refund_and_observability(response_lost: bool) -> None:
    world, scenario = example(response_lost)
    original = scenario.initial_state.model_copy(deep=True)
    run = run_scenario_sync(world, scenario, ReferenceAgent())
    assert run.evaluation.success
    assert run.evaluation.objective_completed
    assert not run.evaluation.constraint_violations
    assert scenario.initial_state == original
    assert len(run.final_state.resources["refund"]) == 1
    assert (
        run.final_state.resources["payment"][scenario.objective.original_payment_id]
        == original.resources["payment"][scenario.objective.original_payment_id]
    )
    usage = run.evaluation.usage
    assert usage.tool_calls == 2 + response_lost
    assert usage.world_steps == usage.tool_calls
    assert usage.retries == usage.failed_calls == int(response_lost)
    assert usage.elapsed_seconds > 0
    assert usage.input_tokens is usage.output_tokens is usage.estimated_cost_usd is None
    assert replay_run(run)
    assert run.events[0].type == "RUN_STARTED"
    assert run.events[-1].type == "RUN_COMPLETED"
    assert [event.sequence for event in run.events] == list(range(len(run.events)))
    assert {"OBSERVATION", "AGENT_DECISION", "WORLD_STATE_CHANGED"}.issubset(
        event.type for event in run.events
    )
    if response_lost:
        actions = [event.payload["action"] for event in run.events if event.type == "TOOL_CALLED"]
        assert actions[1] == actions[2]
        assert sum(event.type == "FAILURE_INJECTED" for event in run.events) == 1


def test_reference_retries_known_preeffect_failure() -> None:
    world, scenario = example()
    refund_tool = next(name for name, action in world.actions.items() if action.kind == "refund")
    scenario.failures = [FailureInjection(action=refund_tool, kind="error_before")]
    run = run_scenario_sync(world, scenario, ReferenceAgent())
    assert run.evaluation.success
    assert run.evaluation.usage.retries == 1
    assert len(run.final_state.resources["refund"]) == 1
    assert replay_run(run)


class StopAgent(ReferenceAgent):
    async def act(self, observation: AgentObservation) -> AgentDecision:
        return AgentDecision(message="All done")


def test_final_message_cannot_claim_success() -> None:
    world, scenario = example()
    run = run_scenario_sync(world, scenario, StopAgent())
    assert not run.evaluation.success
    assert not run.evaluation.objective_completed
    assert run.evaluation.usage.tool_calls == 0
    assert replay_run(run)


class LoopAgent(ReferenceAgent):
    async def act(self, observation: AgentObservation) -> AgentDecision:
        return AgentDecision(
            action=AgentAction(
                tool="list_payments", arguments={"customer_id": observation.visible["customer_id"]}
            )
        )


def test_tool_call_budget_limits_loops() -> None:
    world, scenario = example()
    scenario.max_steps = 3
    run = run_scenario_sync(world, scenario, LoopAgent())
    assert not run.evaluation.success
    assert run.evaluation.termination == "step_limit"
    assert run.evaluation.usage.tool_calls == 3
    assert run.evaluation.usage.retries == 0
    assert replay_run(run)


class WrongToolAgent(ReferenceAgent):
    async def act(self, observation: AgentObservation) -> AgentDecision:
        return AgentDecision(action=AgentAction(tool="delete_everything"))


def test_disallowed_tool_is_rejected_without_effect() -> None:
    world, scenario = example()
    run = run_scenario_sync(world, scenario, WrongToolAgent())
    assert not run.evaluation.success
    assert run.evaluation.termination == "disallowed_action"
    assert run.evaluation.usage.tool_calls == run.evaluation.usage.failed_calls == 1
    assert run.evaluation.usage.world_steps == 0
    assert run.final_state == scenario.initial_state
    assert "disallowed_action:delete_everything" in run.evaluation.constraint_violations
    assert replay_run(run)


class BrokenAgent(ReferenceAgent):
    def __init__(self, phase: str, timeout: bool) -> None:
        super().__init__()
        self.phase = phase
        self.timeout = timeout
        self.shutdown_called = False

    async def fail(self) -> None:
        if self.timeout:
            await asyncio.sleep(1)
        raise RuntimeError("adapter failed")

    async def reset(self) -> None:
        if self.phase == "reset":
            await self.fail()
        await super().reset()

    async def act(self, observation: AgentObservation) -> AgentDecision:
        if self.phase == "act":
            await self.fail()
        return await super().act(observation)

    async def shutdown(self) -> None:
        self.shutdown_called = True
        if self.phase == "shutdown":
            await self.fail()


@pytest.mark.parametrize("phase", ["reset", "act", "shutdown"])
@pytest.mark.parametrize("timeout", [False, True])
def test_agent_failures_have_failed_runs_and_cleanup(phase: str, timeout: bool) -> None:
    world, scenario = example()
    agent = BrokenAgent(phase, timeout)
    run = run_scenario_sync(world, scenario, agent, agent_timeout=0.02)
    assert agent.shutdown_called
    assert not run.evaluation.success
    assert run.evaluation.termination == f"agent_{phase}_{'timeout' if timeout else 'error'}"
    assert any(event.type == "AGENT_ERROR" for event in run.events)
    if phase == "shutdown":
        assert run.evaluation.objective_completed
    assert replay_run(run)


def test_agent_does_not_receive_hidden_scenario_data() -> None:
    world, scenario = example()

    class InspectAgent(ReferenceAgent):
        async def act(self, observation: AgentObservation) -> AgentDecision:
            assert set(type(observation).model_fields) == {
                "task",
                "visible",
                "tools",
                "last_result",
                "steps_remaining",
            }
            assert "initial_state" not in observation.visible
            assert "objective" not in observation.visible
            # Mutating the supplied contracts cannot mutate the world or recorded observation.
            observation.tools.clear()
            observation.visible.clear()
            return AgentDecision(message="inspected")

    run = run_scenario_sync(world, scenario, InspectAgent())
    assert world.actions
    assert scenario.visible
    assert replay_run(run)


def test_parallel_runs_have_independent_world_state() -> None:
    world, scenario = example(response_lost=True)

    async def execute():  # type: ignore[no-untyped-def]
        return await asyncio.gather(
            *(run_scenario(world, scenario, ReferenceAgent()) for _ in range(3))
        )

    runs = asyncio.run(execute())
    assert all(run.evaluation.success for run in runs)
    assert len({run.run_id for run in runs}) == 3
    assert all(run.final_state == runs[0].final_state for run in runs)
    assert scenario.initial_state.resources["refund"] == {}


def test_world_version_mismatch_and_invalid_timeout_are_rejected() -> None:
    world, scenario = example()
    scenario.world_version = "different"
    with pytest.raises(ValueError, match="version"):
        run_scenario_sync(world, scenario, ReferenceAgent())
    scenario.world_version = world.version
    for timeout in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="positive and finite"):
            run_scenario_sync(world, scenario, ReferenceAgent(), timeout)


def test_invalid_agent_metadata_is_a_recorded_failure_with_shutdown() -> None:
    world, scenario = example()

    class MetadataAgent(ReferenceAgent):
        shutdown_called = False

        @property
        def metadata(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("metadata unavailable")

        async def shutdown(self) -> None:
            self.shutdown_called = True

    agent = MetadataAgent()
    run = run_scenario_sync(world, scenario, agent)
    assert agent.shutdown_called
    assert not run.evaluation.success
    assert run.evaluation.termination == "agent_metadata_error"
    assert replay_run(run)


@pytest.mark.parametrize("phase", ["reset", "act", "shutdown"])
def test_invalid_adapter_return_values_fail_the_run(phase: str) -> None:
    world, scenario = example()

    class InvalidAgent(ReferenceAgent):
        async def reset(self) -> None:
            if phase == "reset":
                return "invalid"  # type: ignore[return-value]
            await super().reset()

        async def act(self, observation: AgentObservation) -> AgentDecision:
            if phase == "act":
                return "invalid"  # type: ignore[return-value]
            return await super().act(observation)

        async def shutdown(self) -> None:
            if phase == "shutdown":
                return "invalid"  # type: ignore[return-value]

    run = run_scenario_sync(world, scenario, InvalidAgent())
    assert not run.evaluation.success
    assert run.evaluation.termination == f"agent_{phase}_error"
    assert replay_run(run)


def test_simulator_exception_is_a_failed_counted_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from worldforge.simulation import Simulator

    world, scenario = example()

    def execute(self, action):  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected simulator failure")

    monkeypatch.setattr(Simulator, "execute", execute)
    run = run_scenario_sync(world, scenario, ReferenceAgent())
    assert not run.evaluation.success
    assert run.evaluation.termination == "simulator_error"
    assert run.evaluation.usage.tool_calls == run.evaluation.usage.failed_calls == 1
    assert run.evaluation.usage.world_steps == 0
    assert replay_run(run)


def test_external_cancellation_still_shuts_down_adapter() -> None:
    world, scenario = example()

    class CancellableAgent(ReferenceAgent):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.closed = False

        async def act(self, observation: AgentObservation) -> AgentDecision:
            self.entered.set()
            await asyncio.sleep(10)
            return AgentDecision(message="finished")

        async def shutdown(self) -> None:
            self.closed = True

    async def exercise() -> None:
        agent = CancellableAgent()
        task = asyncio.create_task(run_scenario(world, scenario, agent))
        await agent.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.closed

    asyncio.run(exercise())


def test_mutated_invalid_decision_is_a_recorded_agent_failure() -> None:
    world, scenario = example()

    class MutatingAgent(ReferenceAgent):
        async def act(self, observation: AgentObservation) -> AgentDecision:
            action = AgentAction(tool="list_payments")
            action.arguments["customer_id"] = object()  # type: ignore[assignment]
            return AgentDecision(action=action)

    run = run_scenario_sync(world, scenario, MutatingAgent())
    assert not run.evaluation.success
    assert run.evaluation.termination == "agent_act_error"
    assert replay_run(run)
