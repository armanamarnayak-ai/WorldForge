"""Bounded async execution, transparent evaluation, and agent-free deterministic replay."""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, JsonValue, TypeAdapter

from worldforge.agents import AgentAdapter
from worldforge.models import (
    AgentAction,
    AgentDecision,
    AgentObservation,
    EvaluationResult,
    FailureInjection,
    ResourceUsage,
    Run,
    Scenario,
    ToolResult,
    WorldDefinition,
    WorldEvent,
    WorldState,
)
from worldforge.simulation import Simulator

_METADATA = TypeAdapter(dict[str, JsonValue])
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _json(model: BaseModel) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], model.model_dump(mode="json", by_alias=True))


def _timestamp(sequence: int) -> str:
    return (_EPOCH + timedelta(microseconds=sequence)).isoformat(timespec="microseconds")


def _validate_context(world: WorldDefinition, scenario: Scenario) -> None:
    if scenario.world_id != world.world_id or scenario.world_version != world.version:
        raise ValueError("scenario world identifier/version does not match the world")


def _observation(
    world: WorldDefinition, scenario: Scenario, last: ToolResult | None, calls: int
) -> AgentObservation:
    return AgentObservation(
        task=scenario.task,
        visible=scenario.visible,
        tools={
            name: world.actions[name]
            for name in scenario.allowed_actions
            if name in world.actions and world.actions[name].supported
        },
        last_result=last,
        steps_remaining=max(0, scenario.max_steps - calls),
    ).model_copy(deep=True)


def _allowed(world: WorldDefinition, scenario: Scenario, action: AgentAction) -> bool:
    return (
        action.tool in scenario.allowed_actions
        and action.tool in world.actions
        and world.actions[action.tool].supported
    )


def evaluate_run(
    scenario: Scenario,
    final_state: WorldState,
    termination: str,
    calls: list[tuple[AgentAction, ToolResult]],
    elapsed_seconds: float = 0.0,
    extra_violations: list[str] | None = None,
) -> EvaluationResult:
    """Judge the exact refund and ledger changes; a final message is never proof of success."""
    initial = scenario.initial_state.resources
    final = final_state.resources
    objective = scenario.objective
    violations = list(extra_violations or [])

    def violation(message: str) -> None:
        if message not in violations:
            violations.append(message)

    initial_payments = initial.get("payment", {})
    payments = final.get("payment", {})
    initial_refunds = initial.get("refund", {})
    refunds = final.get("refund", {})
    original = objective.original_payment_id
    duplicate = objective.duplicate_payment_id
    if payments.get(original) != initial_payments.get(original):
        violation("original_payment_changed")
    for resource in sorted(set(initial) | set(final)):
        before_records = initial.get(resource, {})
        after_records = final.get(resource, {})
        if resource not in ("payment", "refund") and before_records != after_records:
            violation(f"unexpected_resource_effect:{resource}")
    if set(payments) != set(initial_payments):
        violation("payment_records_added_or_removed")
    for identifier, before in sorted(initial_payments.items()):
        after = payments.get(identifier)
        if identifier != duplicate:
            if after != before:
                violation(f"unrelated_payment_changed:{identifier}")
        elif after is None or {
            key: value for key, value in after.items() if key not in ("status", "refunded_amount")
        } != {
            key: value for key, value in before.items() if key not in ("status", "refunded_amount")
        }:
            violation("duplicate_payment_identity_changed")
    for identifier, before in sorted(initial_refunds.items()):
        if refunds.get(identifier) != before:
            violation(f"existing_refund_changed:{identifier}")
    additions = {key: value for key, value in refunds.items() if key not in initial_refunds}
    if len(additions) > 1:
        violation("extra_refunds_created")
    totals: dict[str, int] = defaultdict(int)
    for identifier, refund in sorted(additions.items()):
        amount = refund.get("amount")
        payment_id = refund.get("payment_id")
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
            or not isinstance(payment_id, str)
            or payment_id not in payments
            or refund.get("id") != identifier
            or not isinstance(refund.get("idempotency_key"), str)
            or not refund.get("idempotency_key")
        ):
            violation(f"invalid_refund_audit:{identifier}")
            continue
        totals[payment_id] += amount
        if payment_id != duplicate:
            violation("refund_applied_to_wrong_payment")
    for identifier, payment in sorted(payments.items()):
        amount = payment.get("amount")
        refunded = payment.get("refunded_amount", 0)
        before_refunded = initial_payments.get(identifier, {}).get("refunded_amount", 0)
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or not isinstance(refunded, int)
            or isinstance(refunded, bool)
            or not isinstance(before_refunded, int)
            or isinstance(before_refunded, bool)
            or not 0 <= refunded <= amount
        ):
            violation(f"invalid_refund_balance:{identifier}")
            continue
        if refunded - before_refunded != totals.get(identifier, 0):
            violation(f"refund_conservation_failed:{identifier}")
        if refunded > before_refunded:
            status = "refunded" if refunded == amount else "partially_refunded"
            if payment.get("status") != status:
                violation(f"refund_status_inconsistent:{identifier}")
    target = payments.get(duplicate, {})
    before_target = initial_payments.get(duplicate, {})
    objective_completed = (
        len(additions) == 1
        and totals.get(duplicate, 0) == objective.amount
        and before_target.get("duplicate_of") == original
        and original in initial_payments
        and target.get("customer_id") == objective.customer_id
        and isinstance(target.get("refunded_amount"), int)
        and target.get("refunded_amount")
        == cast(int, before_target.get("refunded_amount", 0)) + objective.amount
        and next(iter(additions.values()), {}).get("amount") == objective.amount
        and next(iter(additions.values()), {}).get("payment_id") == duplicate
    )
    retries = sum(
        previous_result.status >= 400 and previous_action == action
        for (previous_action, previous_result), (action, _) in zip(calls, calls[1:], strict=False)
    )
    return EvaluationResult(
        success=objective_completed and not violations and termination == "agent_finished",
        objective_completed=objective_completed,
        constraint_violations=violations,
        termination=termination,
        usage=ResourceUsage(
            tool_calls=len(calls),
            world_steps=final_state.step - scenario.initial_state.step,
            retries=retries,
            failed_calls=sum(result.status >= 400 for _, result in calls),
            elapsed_seconds=elapsed_seconds,
        ),
    )


class _Recorder:
    def __init__(self, run_id: str, world: WorldDefinition, scenario: Scenario) -> None:
        self.run_id = run_id
        self.world = world
        self.scenario = scenario
        self.events: list[WorldEvent] = []

    def emit(self, kind: str, payload: dict[str, JsonValue] | None = None) -> None:
        sequence = len(self.events)
        self.events.append(
            WorldEvent(
                sequence=sequence,
                timestamp=_timestamp(sequence),
                run_id=self.run_id,
                scenario_id=self.scenario.scenario_id,
                world_id=self.world.world_id,
                world_version=self.world.version,
                type=kind,
                payload=payload or {},
            )
        )


def _error_payload(phase: str, error: Exception) -> dict[str, JsonValue]:
    return {
        "phase": phase,
        "error": f"{type(error).__name__}: {error}"[:500],
        "timeout": isinstance(error, TimeoutError),
    }


def _error_termination(payload: dict[str, JsonValue]) -> str:
    return f"agent_{payload['phase']}_{'timeout' if payload['timeout'] else 'error'}"


async def run_scenario(
    world: WorldDefinition,
    scenario: Scenario,
    agent: AgentAdapter,
    agent_timeout: float = 10.0,
) -> Run:
    """Execute trusted adapter callbacks with independent reset/act/shutdown timeouts."""
    _validate_context(world, scenario)
    if not math.isfinite(agent_timeout) or agent_timeout <= 0:
        raise ValueError("agent_timeout must be positive and finite")
    started = time.perf_counter()
    created_at = datetime.now(UTC).isoformat()
    world = world.model_copy(deep=True)
    scenario = scenario.model_copy(deep=True)
    recorder = _Recorder(f"WF-{uuid4().hex}", world, scenario)
    simulator = Simulator(world, scenario.initial_state, scenario.failures)
    calls: list[tuple[AgentAction, ToolResult]] = []
    extra_violations: list[str] = []
    metadata: dict[str, JsonValue] = {}
    metadata_error: Exception | None = None
    try:
        metadata = _METADATA.validate_python(agent.metadata)
    except Exception as error:
        metadata_error = error
    recorder.emit(
        "RUN_STARTED", {"initial_state": _json(scenario.initial_state), "agent_metadata": metadata}
    )
    termination = "agent_finished"
    try:
        if metadata_error is not None:
            payload = _error_payload("metadata", metadata_error)
            recorder.emit("AGENT_ERROR", payload)
            termination = _error_termination(payload)
        else:
            try:
                reset_callback = cast(Callable[[], Awaitable[object]], agent.reset)
                reset_result = await asyncio.wait_for(reset_callback(), timeout=agent_timeout)
                if reset_result is not None:
                    raise TypeError("reset must return None")
                recorder.emit("AGENT_RESET")
            except Exception as error:
                payload = _error_payload("reset", error)
                recorder.emit("AGENT_ERROR", payload)
                termination = _error_termination(payload)
            else:
                last: ToolResult | None = None
                while True:
                    observation = _observation(world, scenario, last, len(calls))
                    recorder.emit("OBSERVATION", {"observation": _json(observation)})
                    try:
                        decision = await asyncio.wait_for(
                            agent.act(observation.model_copy(deep=True)), timeout=agent_timeout
                        )
                        if not isinstance(decision, AgentDecision):
                            raise TypeError("act must return an AgentDecision")
                        # Revalidate mutable nested containers supplied by trusted adapters.
                        # Assignment validation alone cannot catch a mutated arguments dict.
                        decision = AgentDecision.model_validate(
                            decision.model_dump(mode="python", warnings="error")
                        )
                    except Exception as error:
                        payload = _error_payload("act", error)
                        recorder.emit("AGENT_ERROR", payload)
                        termination = _error_termination(payload)
                        break
                    recorder.emit("AGENT_DECISION", {"decision": _json(decision)})
                    if decision.action is None:
                        break
                    if len(calls) >= scenario.max_steps:
                        termination = "step_limit"
                        break
                    action = decision.action
                    before = simulator.state
                    recorder.emit("TOOL_CALLED", {"action": _json(action)})
                    if not _allowed(world, scenario, action):
                        last = ToolResult(
                            status=403, error="Action is not allowed by this scenario"
                        )
                        calls.append((action, last))
                        recorder.emit("TOOL_RETURNED", {"result": _json(last)})
                        extra_violations.append(f"disallowed_action:{action.tool}")
                        termination = "disallowed_action"
                        break
                    try:
                        last = simulator.execute(action)
                    except Exception as error:
                        recorder.emit("SIMULATOR_ERROR", _error_payload("simulator", error))
                        last = ToolResult(status=500, error="Simulator execution failed")
                        calls.append((action, last))
                        recorder.emit("TOOL_RETURNED", {"result": _json(last)})
                        after = simulator.state
                        if before != after:
                            recorder.emit(
                                "WORLD_STATE_CHANGED",
                                {"before": _json(before), "after": _json(after)},
                            )
                        termination = "simulator_error"
                        break
                    calls.append((action, last))
                    if simulator.last_failure is not None:
                        recorder.emit(
                            "FAILURE_INJECTED", {"failure": _json(simulator.last_failure)}
                        )
                    recorder.emit("TOOL_RETURNED", {"result": _json(last)})
                    after = simulator.state
                    if before != after:
                        recorder.emit(
                            "WORLD_STATE_CHANGED", {"before": _json(before), "after": _json(after)}
                        )
    finally:
        try:
            shutdown_callback = cast(Callable[[], Awaitable[object]], agent.shutdown)
            shutdown_result = await asyncio.wait_for(shutdown_callback(), timeout=agent_timeout)
            if shutdown_result is not None:
                raise TypeError("shutdown must return None")
            recorder.emit("AGENT_SHUTDOWN")
        except Exception as error:
            payload = _error_payload("shutdown", error)
            recorder.emit("AGENT_ERROR", payload)
            termination = _error_termination(payload)
    evaluation = evaluate_run(
        scenario,
        simulator.state,
        termination,
        calls,
        elapsed_seconds=time.perf_counter() - started,
        extra_violations=extra_violations,
    )
    for violation in evaluation.constraint_violations:
        recorder.emit("CONSTRAINT_VIOLATED", {"violation": violation})
    recorder.emit("RUN_COMPLETED", {"evaluation": _json(evaluation)})
    return Run(
        run_id=recorder.run_id,
        created_at=created_at,
        world=world,
        scenario=scenario,
        agent_metadata=metadata,
        events=recorder.events,
        final_state=simulator.state,
        evaluation=evaluation,
    )


def run_scenario_sync(
    world: WorldDefinition, scenario: Scenario, agent: AgentAdapter, agent_timeout: float = 10.0
) -> Run:
    return asyncio.run(run_scenario(world, scenario, agent, agent_timeout))


class ReplayError(ValueError):
    """Recorded observations, transitions, outcomes, or event structure are inconsistent."""


class _ReplayCursor:
    def __init__(self, run: Run) -> None:
        self.run = run
        self.index = 0

    def peek(self) -> WorldEvent:
        if self.index >= len(self.run.events):
            raise ReplayError("event history ended prematurely")
        return self.run.events[self.index]

    def expect(self, kind: str, payload: dict[str, JsonValue] | None = None) -> WorldEvent:
        event = self.peek()
        if event.type != kind:
            raise ReplayError(f"event {self.index}: expected {kind}, found {event.type}")
        if payload is not None and event.payload != payload:
            raise ReplayError(f"event {self.index}: {kind} payload does not match replay")
        self.index += 1
        return event

    def error(self, phase: str, kind: str = "AGENT_ERROR") -> str:
        event = self.expect(kind)
        payload = event.payload
        if (
            set(payload) != {"phase", "error", "timeout"}
            or payload.get("phase") != phase
            or not isinstance(payload.get("error"), str)
            or not isinstance(payload.get("timeout"), bool)
        ):
            raise ReplayError("malformed agent error event")
        return _error_termination(payload)


def replay_run(run: Run) -> bool:
    """Reexecute recorded actions without loading or calling an agent implementation."""
    try:
        return _replay(run)
    except ReplayError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ReplayError(f"invalid replay artifact: {error}") from error


def _replay(run: Run) -> bool:
    _validate_context(run.world, run.scenario)
    for sequence, event in enumerate(run.events):
        if (
            event.sequence != sequence
            or event.timestamp != _timestamp(sequence)
            or event.run_id != run.run_id
            or event.scenario_id != run.scenario.scenario_id
            or event.world_id != run.world.world_id
            or event.world_version != run.world.version
        ):
            raise ReplayError(f"event {sequence}: invalid event sequence, clock, or context")
    cursor = _ReplayCursor(run)
    scenario, world = run.scenario, run.world
    simulator = Simulator(world, scenario.initial_state, scenario.failures)
    cursor.expect(
        "RUN_STARTED",
        {"initial_state": _json(scenario.initial_state), "agent_metadata": run.agent_metadata},
    )
    calls: list[tuple[AgentAction, ToolResult]] = []
    extra_violations: list[str] = []
    termination = "agent_finished"
    if cursor.peek().type == "AGENT_ERROR":
        phase = cursor.peek().payload.get("phase")
        if phase not in ("metadata", "reset"):
            raise ReplayError("an initial agent error must concern metadata or reset")
        termination = cursor.error(cast(str, phase))
    else:
        cursor.expect("AGENT_RESET", {})
        last: ToolResult | None = None
        while True:
            observation = _observation(world, scenario, last, len(calls))
            cursor.expect("OBSERVATION", {"observation": _json(observation)})
            if cursor.peek().type == "AGENT_ERROR":
                termination = cursor.error("act")
                break
            event = cursor.expect("AGENT_DECISION")
            if set(event.payload) != {"decision"}:
                raise ReplayError("malformed decision event")
            decision = AgentDecision.model_validate(event.payload["decision"])
            if decision.action is None:
                break
            if len(calls) >= scenario.max_steps:
                termination = "step_limit"
                break
            action = decision.action
            before = simulator.state
            cursor.expect("TOOL_CALLED", {"action": _json(action)})
            if not _allowed(world, scenario, action):
                last = ToolResult(status=403, error="Action is not allowed by this scenario")
                calls.append((action, last))
                cursor.expect("TOOL_RETURNED", {"result": _json(last)})
                extra_violations.append(f"disallowed_action:{action.tool}")
                termination = "disallowed_action"
                break
            try:
                last = simulator.execute(action)
            except Exception as error:
                cursor.expect("SIMULATOR_ERROR", _error_payload("simulator", error))
                last = ToolResult(status=500, error="Simulator execution failed")
                calls.append((action, last))
                cursor.expect("TOOL_RETURNED", {"result": _json(last)})
                after = simulator.state
                if before != after:
                    cursor.expect(
                        "WORLD_STATE_CHANGED", {"before": _json(before), "after": _json(after)}
                    )
                termination = "simulator_error"
                break
            calls.append((action, last))
            if simulator.last_failure is not None:
                failure: FailureInjection = simulator.last_failure
                cursor.expect("FAILURE_INJECTED", {"failure": _json(failure)})
            cursor.expect("TOOL_RETURNED", {"result": _json(last)})
            after = simulator.state
            if before != after:
                cursor.expect(
                    "WORLD_STATE_CHANGED", {"before": _json(before), "after": _json(after)}
                )
    if cursor.peek().type == "AGENT_ERROR":
        termination = cursor.error("shutdown")
    else:
        cursor.expect("AGENT_SHUTDOWN", {})
    evaluation = evaluate_run(
        scenario,
        simulator.state,
        termination,
        calls,
        elapsed_seconds=run.evaluation.usage.elapsed_seconds,
        extra_violations=extra_violations,
    )
    if simulator.state != run.final_state:
        raise ReplayError("final state differs from replayed state")
    if evaluation != run.evaluation:
        raise ReplayError("evaluation differs from replayed evaluation")
    for violation in evaluation.constraint_violations:
        cursor.expect("CONSTRAINT_VIOLATED", {"violation": violation})
    cursor.expect("RUN_COMPLETED", {"evaluation": _json(evaluation)})
    if cursor.index != len(run.events):
        raise ReplayError("events follow the run completion")
    return True
