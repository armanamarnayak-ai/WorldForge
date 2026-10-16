"""Bounded async execution, transparent evaluation, and agent-free deterministic replay."""

from __future__ import annotations

import asyncio
import json
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
    ProviderRecord,
    ResourceReport,
    ResourceUsage,
    Run,
    Scenario,
    ToolResult,
    WorldDefinition,
    WorldEvent,
    WorldState,
)
from worldforge.resources import resource_report, retry_count
from worldforge.simulation import Simulator

_METADATA = TypeAdapter(dict[str, JsonValue])
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_METADATA_BYTES = 64 * 1024
_MAX_DECISION_BYTES = 256 * 1024
_MAX_PROVIDER_RECORDS = 2000
_MAX_PROVIDER_USAGE_BYTES = 1024 * 1024
_MAX_CONTEXT_BYTES = 8 * 1024 * 1024
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_TRACE_BYTES = 16 * 1024 * 1024
_TRACE_FINAL_RESERVE_BYTES = 4 * 1024 * 1024


def _encoded_bytes(value: JsonValue) -> int:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    # Conservatively include indentation when nested in a pretty-printed Run.
    return len(text.encode("utf-8")) + 6 * (text.count("\n") + 1) + 2


def _json(model: BaseModel) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], model.model_dump(mode="json", by_alias=True))


def _timestamp(sequence: int) -> str:
    return (_EPOCH + timedelta(microseconds=sequence)).isoformat(timespec="microseconds")


def _validate_context(world: WorldDefinition, scenario: Scenario) -> None:
    if scenario.world_id != world.world_id or scenario.world_version != world.version:
        raise ValueError("scenario world identifier/version does not match the world")


def _validate_runtime_inputs(world: WorldDefinition, scenario: Scenario) -> None:
    if (
        len(world.world_id.encode("utf-8")) > 256
        or len(world.version.encode("utf-8")) > 128
        or len(scenario.scenario_id.encode("utf-8")) > 256
    ):
        raise ValueError("runtime world/scenario identifiers exceed their serialized byte budgets")
    if _encoded_bytes(_json(world)) + _encoded_bytes(_json(scenario)) > _MAX_CONTEXT_BYTES:
        raise ValueError("world and scenario exceed the 8 MiB serialized runtime context budget")
    if _encoded_bytes(_json(scenario.initial_state)) > _MAX_STATE_BYTES:
        raise ValueError("initial state exceeds the 4 MiB serialized runtime state budget")


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
    detect_unsafe_retries: bool = False,
) -> EvaluationResult:
    """Judge the exact refund and ledger changes; a final message is never proof of success."""
    initial = scenario.initial_state.resources
    final = final_state.resources
    objective = scenario.objective
    violations = list(extra_violations or [])

    def violation(message: str) -> None:
        if message not in violations:
            violations.append(message)

    namespace = scenario.visible.get("payments_namespace")
    payment_resource = f"{namespace}.payment" if isinstance(namespace, str) else "payment"
    refund_resource = f"{namespace}.refund" if isinstance(namespace, str) else "refund"
    initial_payments = initial.get(payment_resource, {})
    payments = final.get(payment_resource, {})
    initial_refunds = initial.get(refund_resource, {})
    refunds = final.get(refund_resource, {})
    original = objective.original_payment_id
    duplicate = objective.duplicate_payment_id
    if payments.get(original) != initial_payments.get(original):
        violation("original_payment_changed")
    for resource in sorted(set(initial) | set(final)):
        before_records = initial.get(resource, {})
        after_records = final.get(resource, {})
        if resource not in (payment_resource, refund_resource) and before_records != after_records:
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
    if detect_unsafe_retries:
        retries = retry_count(calls)
        for (prior_action, result), (retry_action, _) in zip(calls, calls[1:], strict=False):
            if (
                result.status == 504
                and prior_action.tool == retry_action.tool
                and isinstance(prior_action.arguments.get("idempotency_key"), str)
                and prior_action.arguments.get("payment_id")
                == retry_action.arguments.get("payment_id")
                and prior_action.arguments.get("amount") == retry_action.arguments.get("amount")
                and prior_action.arguments.get("idempotency_key")
                != retry_action.arguments.get("idempotency_key")
            ):
                violation("unsafe_retry_key_changed")
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
        self.trace_bytes = 0

    def batch_size(self, batch: list[tuple[str, dict[str, JsonValue]]]) -> int:
        return sum(
            _encoded_bytes(_json(self._event(kind, payload, len(self.events) + offset)))
            for offset, (kind, payload) in enumerate(batch)
        )

    def fits(self, batch: list[tuple[str, dict[str, JsonValue]]]) -> bool:
        return self.trace_bytes + self.batch_size(batch) <= (
            _MAX_TRACE_BYTES - _TRACE_FINAL_RESERVE_BYTES
        )

    def budget_payload(
        self,
        phase: str,
        batch: list[tuple[str, dict[str, JsonValue]]],
        *,
        state_bytes: int = 0,
    ) -> dict[str, JsonValue]:
        return {
            "phase": phase,
            "trace_budget_bytes": _MAX_TRACE_BYTES,
            "trace_bytes": self.trace_bytes,
            "pending_bytes": self.batch_size(batch),
            "state_budget_bytes": _MAX_STATE_BYTES,
            "state_bytes": state_bytes,
        }

    def _event(self, kind: str, payload: dict[str, JsonValue], sequence: int) -> WorldEvent:
        return WorldEvent(
            sequence=sequence,
            timestamp=_timestamp(sequence),
            run_id=self.run_id,
            scenario_id=self.scenario.scenario_id,
            world_id=self.world.world_id,
            world_version=self.world.version,
            type=kind,
            payload=payload,
        )

    def emit(self, kind: str, payload: dict[str, JsonValue] | None = None) -> None:
        event = self._event(kind, payload or {}, len(self.events))
        self.trace_bytes += _encoded_bytes(_json(event))
        self.events.append(event)

    def emit_batch(self, batch: list[tuple[str, dict[str, JsonValue]]]) -> None:
        for kind, payload in batch:
            self.emit(kind, payload)


def _error_payload(phase: str, error: Exception) -> dict[str, JsonValue]:
    return {
        "phase": phase,
        "error": f"{type(error).__name__}: {error}"[:500],
        "timeout": isinstance(error, TimeoutError),
    }


def _error_termination(payload: dict[str, JsonValue]) -> str:
    return f"agent_{payload['phase']}_{'timeout' if payload['timeout'] else 'error'}"


def _bounded_json(value: JsonValue, limit: int, label: str) -> None:
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit}-byte runtime limit")


def _drain_provider_records(agent: AgentAdapter) -> list[ProviderRecord]:
    callback = getattr(agent, "drain_provider_records", None)
    if callback is None:
        return []
    records = callback()
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("provider usage drain must return at most 1000 typed records")
    validated: list[ProviderRecord] = []
    for record in records:
        if not isinstance(record, ProviderRecord):
            raise TypeError("provider usage must contain ProviderRecord values")
        record = ProviderRecord.model_validate(record.model_dump(mode="python", warnings="error"))
        _bounded_json(_json(record), 16 * 1024, "provider record")
        validated.append(record)
    return validated


def _resource_payload(report: ResourceReport) -> dict[str, JsonValue]:
    payload = _json(report)
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict)
    # Wall-clock time is measured in the artifact and checked against evaluation;
    # deterministic events contain only counters, virtual time, and usage reports.
    metrics.pop("actual_runtime_latency_ms", None)
    return {"report": payload}


def _failure_payloads(simulator: Simulator) -> list[dict[str, JsonValue]]:
    return [
        {"failure": _json(failure), "details": details}
        for failure, details in zip(
            simulator.last_failures, simulator.last_failure_details, strict=True
        )
    ]


def _stage_tool(
    world: WorldDefinition, scenario: Scenario, simulator: Simulator, action: AgentAction
) -> tuple[Simulator, ToolResult, list[tuple[str, dict[str, JsonValue]]], str | None]:
    """Build a complete synthetic transition before committing its bounded trace."""
    batch: list[tuple[str, dict[str, JsonValue]]] = [("TOOL_CALLED", {"action": _json(action)})]
    if not _allowed(world, scenario, action):
        result = ToolResult(status=403, error="Action is not allowed by this scenario")
        batch.append(("TOOL_RETURNED", {"result": _json(result)}))
        return simulator, result, batch, "disallowed_action"
    before = simulator.state
    proposed = simulator.fork()
    termination: str | None = None
    try:
        result = proposed.execute(action)
    except Exception as error:
        batch.append(("SIMULATOR_ERROR", _error_payload("simulator", error)))
        result = ToolResult(status=500, error="Simulator execution failed")
        termination = "simulator_error"
    else:
        batch.extend(("FAILURE_INJECTED", payload) for payload in _failure_payloads(proposed))
        batch.append(
            (
                "SIMULATED_LATENCY",
                {
                    "latency_ms": proposed.last_latency_ms,
                    "simulated_elapsed_ms": proposed.simulated_elapsed_ms,
                },
            )
        )
    batch.append(("TOOL_RETURNED", {"result": _json(result)}))
    after = proposed.state
    if before != after:
        batch.append(("WORLD_STATE_CHANGED", {"before": _json(before), "after": _json(after)}))
    return proposed, result, batch, termination


def _rejected_decision_batch(value: JsonValue) -> list[tuple[str, dict[str, JsonValue]]]:
    if not isinstance(value, list) or not value or len(value) > 1001:
        raise ReplayError("malformed rejected decision batch")
    batch: list[tuple[str, dict[str, JsonValue]]] = []
    for offset, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"type", "payload"}:
            raise ReplayError("malformed rejected event")
        expected = "AGENT_DECISION" if offset == len(value) - 1 else "PROVIDER_USAGE"
        payload = item["payload"]
        if item["type"] != expected or not isinstance(payload, dict):
            raise ReplayError("invalid rejected decision event ordering")
        if expected == "AGENT_DECISION":
            if set(payload) != {"decision"}:
                raise ReplayError("malformed rejected decision")
            decision = AgentDecision.model_validate(payload["decision"])
            _bounded_json(_json(decision), _MAX_DECISION_BYTES, "agent decision")
            if payload != {"decision": _json(decision)}:
                raise ReplayError("noncanonical rejected decision")
        else:
            if set(payload) != {"record"}:
                raise ReplayError("malformed rejected provider usage")
            record = ProviderRecord.model_validate(payload["record"])
            _bounded_json(_json(record), 16 * 1024, "provider record")
            if payload != {"record": _json(record)}:
                raise ReplayError("noncanonical rejected provider usage")
        batch.append((expected, payload))
    return batch


async def run_scenario(
    world: WorldDefinition,
    scenario: Scenario,
    agent: AgentAdapter,
    agent_timeout: float = 10.0,
) -> Run:
    """Execute trusted adapter callbacks with independent reset/act/shutdown timeouts."""
    _validate_context(world, scenario)
    _validate_runtime_inputs(world, scenario)
    if not math.isfinite(agent_timeout) or agent_timeout <= 0:
        raise ValueError("agent_timeout must be positive and finite")
    started = time.perf_counter()
    deadline = started + scenario.max_runtime_seconds
    created_at = datetime.now(UTC).isoformat()
    world = world.model_copy(deep=True)
    scenario = scenario.model_copy(deep=True)
    recorder = _Recorder(f"WF-{uuid4().hex}", world, scenario)
    simulator = Simulator(
        world,
        scenario.initial_state,
        scenario.failures,
        max_simulated_ms=scenario.simulated_latency_budget_ms,
    )
    calls: list[tuple[AgentAction, ToolResult]] = []
    extra_violations: list[str] = []
    metadata: dict[str, JsonValue] = {}
    metadata_error: Exception | None = None
    provider_records: list[ProviderRecord] = []
    provider_usage_complete = True
    provider_bytes = 0
    try:
        metadata = _METADATA.validate_python(agent.metadata)
        _bounded_json(metadata, _MAX_METADATA_BYTES, "agent metadata")
    except Exception as error:
        metadata_error = error
        metadata = {}
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
                reset_result = await asyncio.wait_for(
                    reset_callback(),
                    timeout=min(agent_timeout, max(0, deadline - time.perf_counter())),
                )
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
                    observation_batch: list[tuple[str, dict[str, JsonValue]]] = [
                        ("OBSERVATION", {"observation": _json(observation)})
                    ]
                    if not recorder.fits(observation_batch):
                        recorder.emit(
                            "TRACE_BUDGET_EXHAUSTED",
                            recorder.budget_payload("observation", observation_batch),
                        )
                        termination = "artifact_limit"
                        break
                    recorder.emit_batch(observation_batch)
                    if time.perf_counter() >= deadline:
                        recorder.emit(
                            "RUNTIME_BUDGET_EXHAUSTED",
                            {"budget_seconds": scenario.max_runtime_seconds},
                        )
                        termination = "runtime_limit"
                        break
                    try:
                        decision = await asyncio.wait_for(
                            agent.act(observation.model_copy(deep=True)),
                            timeout=min(agent_timeout, max(0, deadline - time.perf_counter())),
                        )
                        if not isinstance(decision, AgentDecision):
                            raise TypeError("act must return an AgentDecision")
                        # Revalidate mutable nested containers supplied by trusted adapters.
                        # Assignment validation alone cannot catch a mutated arguments dict.
                        decision = AgentDecision.model_validate(
                            decision.model_dump(mode="python", warnings="error")
                        )
                        _bounded_json(_json(decision), _MAX_DECISION_BYTES, "agent decision")
                        reported = _drain_provider_records(agent)
                        if len(provider_records) + len(reported) > _MAX_PROVIDER_RECORDS:
                            raise ValueError("provider usage record budget exceeded")
                        reported_bytes = sum(_encoded_bytes(_json(record)) for record in reported)
                        if provider_bytes + reported_bytes > _MAX_PROVIDER_USAGE_BYTES:
                            raise ValueError(
                                "provider usage exceeds the 1 MiB serialized byte budget"
                            )
                    except Exception as error:
                        provider_usage_complete = False
                        payload = _error_payload("act", error)
                        recorder.emit("AGENT_ERROR", payload)
                        termination = _error_termination(payload)
                        break
                    decision_batch: list[tuple[str, dict[str, JsonValue]]] = [
                        ("PROVIDER_USAGE", {"record": _json(record)}) for record in reported
                    ]
                    decision_batch.append(("AGENT_DECISION", {"decision": _json(decision)}))
                    if not recorder.fits(decision_batch):
                        provider_records.extend(reported)
                        provider_bytes += reported_bytes
                        budget_payload = recorder.budget_payload("decision", decision_batch)
                        budget_payload["rejected_events"] = [
                            {"type": kind, "payload": payload} for kind, payload in decision_batch
                        ]
                        recorder.emit(
                            "TRACE_BUDGET_EXHAUSTED",
                            budget_payload,
                        )
                        termination = "artifact_limit"
                        break
                    provider_records.extend(reported)
                    provider_bytes += reported_bytes
                    recorder.emit_batch(decision_batch)
                    if decision.action is None:
                        break
                    if len(calls) >= scenario.max_steps:
                        termination = "step_limit"
                        break
                    action = decision.action
                    proposed, result, tool_batch, tool_termination = _stage_tool(
                        world, scenario, simulator, action
                    )
                    state_bytes = _encoded_bytes(_json(proposed.state))
                    if not recorder.fits(tool_batch) or state_bytes > _MAX_STATE_BYTES:
                        recorder.emit(
                            "TRACE_BUDGET_EXHAUSTED",
                            recorder.budget_payload("tool", tool_batch, state_bytes=state_bytes),
                        )
                        termination = "artifact_limit"
                        break
                    simulator, last = proposed, result
                    calls.append((action, last))
                    recorder.emit_batch(tool_batch)
                    if tool_termination == "disallowed_action":
                        extra_violations.append(f"disallowed_action:{action.tool}")
                    if tool_termination is not None:
                        termination = tool_termination
                        break
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
        detect_unsafe_retries=True,
    )
    report = resource_report(
        calls,
        world_steps=evaluation.usage.world_steps,
        max_steps=scenario.max_steps,
        simulated_latency_ms=simulator.simulated_elapsed_ms,
        elapsed_seconds=evaluation.usage.elapsed_seconds,
        providers=provider_records,
        provider_usage_complete=provider_usage_complete,
    )
    recorder.emit("RESOURCE_USAGE", _resource_payload(report))
    for warning in report.warnings:
        recorder.emit("RESOURCE_WARNING", {"warning": _json(warning)})
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
        resource_report=report,
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
        self.trace_bytes = 0

    def recorder(self) -> _Recorder:
        recorder = _Recorder(self.run.run_id, self.run.world, self.run.scenario)
        recorder.events = self.run.events[: self.index]
        recorder.trace_bytes = self.trace_bytes
        return recorder

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
        self.trace_bytes += _encoded_bytes(_json(event))
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
    modern = run.schema_version == "1.1"
    if modern:
        _validate_runtime_inputs(world, scenario)
        _bounded_json(run.agent_metadata, _MAX_METADATA_BYTES, "agent metadata")
    simulator = Simulator(
        world,
        scenario.initial_state,
        scenario.failures,
        max_simulated_ms=scenario.simulated_latency_budget_ms,
    )
    cursor.expect(
        "RUN_STARTED",
        {"initial_state": _json(scenario.initial_state), "agent_metadata": run.agent_metadata},
    )
    calls: list[tuple[AgentAction, ToolResult]] = []
    provider_records: list[ProviderRecord] = []
    provider_usage_complete = True
    provider_bytes = 0
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
            observation_batch: list[tuple[str, dict[str, JsonValue]]] = [
                ("OBSERVATION", {"observation": _json(observation)})
            ]
            if modern:
                recorder = cursor.recorder()
                if cursor.peek().type == "TRACE_BUDGET_EXHAUSTED":
                    if recorder.fits(observation_batch):
                        raise ReplayError("trace budget exhausted before its limit")
                    cursor.expect(
                        "TRACE_BUDGET_EXHAUSTED",
                        recorder.budget_payload("observation", observation_batch),
                    )
                    termination = "artifact_limit"
                    break
                if not recorder.fits(observation_batch):
                    raise ReplayError("observation exceeds the trace budget")
            cursor.expect("OBSERVATION", observation_batch[0][1])
            if modern and cursor.peek().type == "RUNTIME_BUDGET_EXHAUSTED":
                cursor.expect(
                    "RUNTIME_BUDGET_EXHAUSTED", {"budget_seconds": scenario.max_runtime_seconds}
                )
                termination = "runtime_limit"
                break
            if cursor.peek().type == "AGENT_ERROR":
                termination = cursor.error("act")
                provider_usage_complete = False
                break
            if modern and cursor.peek().type == "TRACE_BUDGET_EXHAUSTED":
                payload = cursor.peek().payload
                decision_batch = _rejected_decision_batch(payload.get("rejected_events"))
                recorder = cursor.recorder()
                if recorder.fits(decision_batch):
                    raise ReplayError("rejected decision does not exceed the trace budget")
                expected_payload = recorder.budget_payload("decision", decision_batch)
                expected_payload["rejected_events"] = payload["rejected_events"]
                cursor.expect("TRACE_BUDGET_EXHAUSTED", expected_payload)
                for _, usage_payload in decision_batch[:-1]:
                    record = ProviderRecord.model_validate(usage_payload["record"])
                    provider_records.append(record)
                    provider_bytes += _encoded_bytes(_json(record))
                if (
                    len(provider_records) > _MAX_PROVIDER_RECORDS
                    or provider_bytes > _MAX_PROVIDER_USAGE_BYTES
                ):
                    raise ReplayError("rejected provider usage exceeds its budget")
                termination = "artifact_limit"
                break
            decision_recorder = cursor.recorder()
            decision_events: list[tuple[str, dict[str, JsonValue]]] = []
            while modern and cursor.peek().type == "PROVIDER_USAGE":
                usage_event = cursor.expect("PROVIDER_USAGE")
                if set(usage_event.payload) != {"record"}:
                    raise ReplayError("malformed provider usage event")
                record = ProviderRecord.model_validate(usage_event.payload["record"])
                _bounded_json(_json(record), 16 * 1024, "provider record")
                if usage_event.payload != {"record": _json(record)}:
                    raise ReplayError("noncanonical provider usage event")
                provider_records.append(record)
                provider_bytes += _encoded_bytes(_json(record))
                if (
                    len(provider_records) > _MAX_PROVIDER_RECORDS
                    or provider_bytes > _MAX_PROVIDER_USAGE_BYTES
                ):
                    raise ReplayError("provider usage record budget exceeded")
                decision_events.append(("PROVIDER_USAGE", usage_event.payload))
                if len(decision_events) > 1000:
                    raise ReplayError("provider usage drain exceeds 1000 records")
            event = cursor.expect("AGENT_DECISION")
            if set(event.payload) != {"decision"}:
                raise ReplayError("malformed decision event")
            decision = AgentDecision.model_validate(event.payload["decision"])
            if modern:
                _bounded_json(_json(decision), _MAX_DECISION_BYTES, "agent decision")
                if event.payload != {"decision": _json(decision)}:
                    raise ReplayError("noncanonical decision event")
                decision_events.append(("AGENT_DECISION", event.payload))
                if not decision_recorder.fits(decision_events):
                    raise ReplayError("decision exceeds the trace budget")
            if decision.action is None:
                break
            if len(calls) >= scenario.max_steps:
                termination = "step_limit"
                break
            action = decision.action
            if modern:
                proposed, result, tool_batch, tool_termination = _stage_tool(
                    world, scenario, simulator, action
                )
                state_bytes = _encoded_bytes(_json(proposed.state))
                recorder = cursor.recorder()
                admitted = recorder.fits(tool_batch) and state_bytes <= _MAX_STATE_BYTES
                if cursor.peek().type == "TRACE_BUDGET_EXHAUSTED":
                    if admitted:
                        raise ReplayError("tool transition fits the trace and state budgets")
                    cursor.expect(
                        "TRACE_BUDGET_EXHAUSTED",
                        recorder.budget_payload("tool", tool_batch, state_bytes=state_bytes),
                    )
                    termination = "artifact_limit"
                    break
                if not admitted:
                    raise ReplayError("tool transition exceeds the trace or state budget")
                for kind, tool_payload in tool_batch:
                    cursor.expect(kind, tool_payload)
                simulator, last = proposed, result
                calls.append((action, last))
                if tool_termination == "disallowed_action":
                    extra_violations.append(f"disallowed_action:{action.tool}")
                if tool_termination is not None:
                    termination = tool_termination
                    break
                continue
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
            if modern:
                for payload in _failure_payloads(simulator):
                    cursor.expect("FAILURE_INJECTED", payload)
                cursor.expect(
                    "SIMULATED_LATENCY",
                    {
                        "latency_ms": simulator.last_latency_ms,
                        "simulated_elapsed_ms": simulator.simulated_elapsed_ms,
                    },
                )
            elif simulator.last_failure is not None:
                cursor.expect("FAILURE_INJECTED", {"failure": _json(simulator.last_failure)})
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
        detect_unsafe_retries=modern,
    )
    if simulator.state != run.final_state:
        raise ReplayError("final state differs from replayed state")
    if evaluation != run.evaluation:
        raise ReplayError("evaluation differs from replayed evaluation")
    if modern:
        report = resource_report(
            calls,
            world_steps=evaluation.usage.world_steps,
            max_steps=scenario.max_steps,
            simulated_latency_ms=simulator.simulated_elapsed_ms,
            elapsed_seconds=evaluation.usage.elapsed_seconds,
            providers=provider_records,
            provider_usage_complete=provider_usage_complete,
        )
        if report != run.resource_report:
            raise ReplayError("resource report differs from replayed measurements")
        cursor.expect("RESOURCE_USAGE", _resource_payload(report))
        for warning in report.warnings:
            cursor.expect("RESOURCE_WARNING", {"warning": _json(warning)})
    for violation in evaluation.constraint_violations:
        cursor.expect("CONSTRAINT_VIOLATED", {"violation": violation})
    cursor.expect("RUN_COMPLETED", {"evaluation": _json(evaluation)})
    if cursor.index != len(run.events):
        raise ReplayError("events follow the run completion")
    if modern and cursor.trace_bytes > _MAX_TRACE_BYTES:
        raise ReplayError("completed trace exceeds its serialized byte budget")
    return True
