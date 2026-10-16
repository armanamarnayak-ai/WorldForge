from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from worldforge.agents import ReferenceAgent, VulnerableRefundAgent
from worldforge.compiler import compile_openapi
from worldforge.models import (
    AgentDecision,
    AgentObservation,
    FailureCriterion,
    FailureSpec,
    SearchSpec,
)
from worldforge.oracle import (
    criterion_matches,
    default_search_spec,
    falsify_sync,
    reproduce_certificate_sync,
)
from worldforge.runtime import replay_run

EXAMPLE = Path(__file__).parents[1] / "examples/payments/openapi.yaml"


def test_oracle_discovers_minimizes_verifies_and_replays_real_unsafe_retry() -> None:
    world = compile_openapi(EXAMPLE)
    outcome = falsify_sync(
        world, default_search_spec(world), VulnerableRefundAgent, {"agent": "vulnerable"}
    )
    assert outcome.status == "certified"
    certificate = outcome.certificate
    assert certificate is not None
    assert len(certificate.original_conditions) == 2
    assert [fault.kind for fault in certificate.minimized_conditions] == ["response_lost"]
    assert certificate.classification == "unsafe_retry"
    assert certificate.severity == "HIGH"
    assert certificate.reproduction_count == certificate.successful_reproduction_count == 3
    assert certificate.reproduction_rate == 1.0
    assert certificate.audit.one_minimal
    assert certificate.audit.candidate_runs == 2
    assert certificate.audit.minimization_runs == 1
    assert certificate.audit.verification_runs == 3
    assert certificate.audit.total_runs == len(outcome.runs) == 6
    assert certificate.resource_report is not None
    assert certificate.resource_report.metrics["retries"].value == 1
    assert "unsafe_retry_key_changed" in certificate.evaluation.constraint_violations
    assert certificate.evaluation.objective_completed
    assert all(len(run.final_state.resources["refund"]) == 1 for run in outcome.runs)
    assert all(replay_run(run) for run in outcome.runs)
    assert all(run.certificate_id == certificate.certificate_id for run in outcome.runs)
    assert certificate.run_ids == [run.run_id for run in outcome.runs]
    assert certificate.events == outcome.events
    assert [event.sequence for event in outcome.events] == list(range(len(outcome.events)))
    assert {
        "SEARCH_STARTED",
        "FAILURE_DISCOVERED",
        "MINIMIZATION_STEP",
        "CERTIFICATE_CREATED",
        "VERIFICATION_RUN",
        "SEARCH_COMPLETED",
    }.issubset(e.type for e in outcome.events)


def test_safe_agent_does_not_meet_unsafe_criterion_in_same_search_space() -> None:
    world = compile_openapi(EXAMPLE)
    outcome = falsify_sync(world, default_search_spec(world), ReferenceAgent)
    assert outcome.status == "exhausted"
    assert outcome.certificate is None
    assert outcome.audit.candidate_runs == 4
    assert outcome.audit.verification_runs == 0
    assert not outcome.audit.budget_exhausted  # Finite space exhausted naturally.
    assert all(run.evaluation.success and replay_run(run) for run in outcome.runs)


def test_search_order_and_results_repeat_while_run_ids_are_fresh() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    first = falsify_sync(world, spec, VulnerableRefundAgent)
    second = falsify_sync(world, spec, VulnerableRefundAgent)
    assert first.audit == second.audit
    assert [run.scenario for run in first.runs] == [run.scenario for run in second.runs]
    assert [
        (run.scenario.scenario_id, run.scenario.seed, run.scenario.failures) for run in first.runs
    ] == [
        (run.scenario.scenario_id, run.scenario.seed, run.scenario.failures) for run in second.runs
    ]
    assert [run.final_state for run in first.runs] == [run.final_state for run in second.runs]
    assert not set(run.run_id for run in first.runs) & set(run.run_id for run in second.runs)


def test_scenario_identity_covers_world_version_and_runtime_budget() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    original = falsify_sync(world, spec, VulnerableRefundAgent)
    world.version = "2"
    changed_world = falsify_sync(world, spec, VulnerableRefundAgent)
    assert original.runs[0].scenario.scenario_id != changed_world.runs[0].scenario.scenario_id
    spec.max_seconds = 120
    changed_budget = falsify_sync(world, spec, VulnerableRefundAgent)
    assert changed_world.runs[0].scenario.max_runtime_seconds == 15
    assert changed_budget.runs[0].scenario.max_runtime_seconds == 30
    assert changed_world.runs[0].scenario.scenario_id != changed_budget.runs[0].scenario.scenario_id


def test_scenario_identity_covers_world_schema_without_a_version_change() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    original = falsify_sync(world, spec, VulnerableRefundAgent)
    world.title += " revised schema"
    changed = falsify_sync(world, spec, VulnerableRefundAgent)
    assert original.runs[0].scenario.scenario_id != changed.runs[0].scenario.scenario_id


def test_shared_run_budget_includes_minimization_and_verification() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    spec.max_runs = 3
    outcome = falsify_sync(world, spec, VulnerableRefundAgent)
    assert outcome.status == "unverified_failure"
    assert outcome.certificate is None
    assert outcome.audit.total_runs == len(outcome.runs) == 3
    assert outcome.audit.budget_exhausted
    assert outcome.audit.verification_runs == 0


def test_candidate_budget_is_not_a_fake_success() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    spec.max_candidates = 1
    outcome = falsify_sync(world, spec, VulnerableRefundAgent)
    assert outcome.status == "exhausted"
    assert outcome.certificate is None
    assert len(outcome.runs) == 1
    assert outcome.audit.budget_exhausted


class StopAgent(ReferenceAgent):
    async def act(self, observation: AgentObservation) -> AgentDecision:
        return AgentDecision(message="Stopped without refunding.")


def test_zero_condition_objective_failure_is_honestly_certified() -> None:
    world = compile_openapi(EXAMPLE)
    spec = SearchSpec(criterion=FailureCriterion(kind="objective_failure"))
    outcome = falsify_sync(world, spec, StopAgent)
    assert outcome.certificate is not None
    assert outcome.certificate.minimized_conditions == []
    assert outcome.certificate.original_conditions == []
    assert outcome.certificate.classification == "objective_failure"
    assert outcome.certificate.severity == "MEDIUM"
    assert outcome.audit.one_minimal
    assert outcome.audit.candidate_runs == 1
    assert outcome.audit.total_runs == 4


def test_candidate_order_is_baseline_for_all_seeds_then_full_pool() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    spec.seeds = [5, 7]
    outcome = falsify_sync(world, spec, VulnerableRefundAgent)
    assert [(run.scenario.seed, len(run.scenario.failures)) for run in outcome.runs[:3]] == [
        (5, 0),
        (7, 0),
        (5, 2),
    ]


class SlowAgent(ReferenceAgent):
    async def act(self, observation: AgentObservation) -> AgentDecision:
        await asyncio.sleep(10)
        return AgentDecision()


def test_shared_wall_clock_budget_bounds_cooperative_async_agents() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    spec.max_seconds = 0.05
    spec.agent_timeout = 0.01
    started = time.perf_counter()
    outcome = falsify_sync(world, spec, SlowAgent)
    assert time.perf_counter() - started < 0.5
    assert outcome.status == "exhausted"
    assert outcome.certificate is None
    assert outcome.audit.budget_exhausted
    assert all(
        run.evaluation.termination in {"agent_act_timeout", "agent_reset_timeout", "runtime_limit"}
        for run in outcome.runs
    )


def test_failed_verification_never_emits_certificate() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    instances = 0

    def factory() -> ReferenceAgent:
        nonlocal instances
        instances += 1
        return VulnerableRefundAgent() if instances <= 3 else ReferenceAgent()

    outcome = falsify_sync(world, spec, factory)
    assert outcome.status == "unverified_failure"
    assert outcome.certificate is None
    assert outcome.audit.verification_runs == 3
    assert not outcome.audit.budget_exhausted
    assert all(e.type != "CERTIFICATE_CREATED" for e in outcome.events)


def test_reproduction_uses_explicit_agent_and_preserves_stored_certificate() -> None:
    world = compile_openapi(EXAMPLE)
    outcome = falsify_sync(world, default_search_spec(world), VulnerableRefundAgent)
    certificate = outcome.certificate
    assert certificate is not None
    snapshot = certificate.model_dump_json()
    unsafe = reproduce_certificate_sync(certificate, VulnerableRefundAgent)
    safe = reproduce_certificate_sync(certificate, ReferenceAgent)
    assert sum(criterion_matches(run, certificate.criterion) for run in unsafe) == 3
    assert sum(criterion_matches(run, certificate.criterion) for run in safe) == 0
    assert all(run.evaluation.success for run in safe)
    assert all(replay_run(run) for run in unsafe + safe)
    assert all(run.certificate_id == certificate.certificate_id for run in unsafe + safe)
    assert certificate.model_dump_json() == snapshot


@pytest.mark.parametrize("criterion", ["constraint_violation", "any_failure"])
def test_explicit_constraint_and_any_failure_criteria(criterion: str) -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    spec.criterion = FailureCriterion(kind=criterion)
    assert falsify_sync(world, spec, VulnerableRefundAgent).certificate is not None


def test_excessive_retry_criterion_reads_changed_key_retries_from_observatory() -> None:
    world = compile_openapi(EXAMPLE)
    spec = default_search_spec(world)
    spec.criterion = FailureCriterion(kind="excessive_retries", retry_limit=0)
    outcome = falsify_sync(world, spec, VulnerableRefundAgent)
    assert outcome.certificate is not None
    assert outcome.certificate.resource_report is not None
    assert outcome.certificate.resource_report.metrics["retries"].value == 1


def test_invalid_pool_is_rejected_before_agent_creation() -> None:
    world = compile_openapi(EXAMPLE)
    spec = SearchSpec(conditions=[FailureSpec(action="not_a_tool", kind="response_lost")])

    def factory() -> ReferenceAgent:
        raise AssertionError("invalid input must not instantiate an adapter")

    with pytest.raises(ValueError, match="unknown"):
        falsify_sync(world, spec, factory)


@pytest.mark.parametrize("count", [0, 21, True, 1.5])
def test_reproduction_count_is_bounded(count: object) -> None:
    world = compile_openapi(EXAMPLE)
    certificate = falsify_sync(world, default_search_spec(world), VulnerableRefundAgent).certificate
    assert certificate is not None
    with pytest.raises(ValueError, match="reproduction count"):
        reproduce_certificate_sync(certificate, ReferenceAgent, count=count)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [10**400, True, "2", float("inf")])
def test_explicit_reproduction_timeout_is_validated_before_conversion(timeout: object) -> None:
    world = compile_openapi(EXAMPLE)
    certificate = falsify_sync(world, default_search_spec(world), VulnerableRefundAgent).certificate
    assert certificate is not None
    with pytest.raises(ValueError, match="agent timeout"):
        reproduce_certificate_sync(
            certificate,
            ReferenceAgent,
            agent_timeout=timeout,  # type: ignore[arg-type]
        )
