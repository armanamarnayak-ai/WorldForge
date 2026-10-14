from __future__ import annotations

import random
from pathlib import Path

import pytest
from test_simulation import payments_world

from worldforge.compiler import compile_openapi
from worldforge.models import AgentAction, Scenario, WorldDefinition
from worldforge.scenarios import generate_scenarios
from worldforge.simulation import Simulator


def test_seeded_generation_is_reproducible_and_leaves_global_rng_unchanged() -> None:
    random.seed(137)
    before = random.getstate()
    first = generate_scenarios(payments_world(), count=5, seed=42)
    assert random.getstate() == before
    second = generate_scenarios(payments_world(), count=5, seed=42)
    assert first == second
    assert len({scenario.scenario_id for scenario in first}) == 5
    assert first != generate_scenarios(payments_world(), count=5, seed=43)
    assert len({scenario.objective.amount for scenario in first}) > 1
    for scenario in first:
        assert Scenario.model_validate_json(scenario.model_dump_json()) == scenario


def test_failure_variants_have_distinct_scenario_identity() -> None:
    world = payments_world()
    normal = generate_scenarios(world, count=1, seed=42)[0]
    lost = generate_scenarios(world, count=1, seed=42, response_lost=True)[0]
    assert normal.scenario_id != lost.scenario_id
    assert normal.initial_state == lost.initial_state
    assert normal.objective == lost.objective


def test_objective_is_hidden_but_discoverable_by_listing_payments() -> None:
    world = payments_world()
    scenario = generate_scenarios(world, count=1, seed=9)[0]
    objective = scenario.objective
    assert scenario.visible == {
        "customer_id": objective.customer_id,
        "reported_amount": objective.amount,
    }
    assert objective.original_payment_id not in scenario.task
    assert objective.duplicate_payment_id not in scenario.task
    assert scenario.allowed_actions == sorted(world.actions)
    sim = Simulator(world, scenario.initial_state)
    result = sim.execute(
        AgentAction(
            tool="list_payments",
            arguments={"customer_id": objective.customer_id},
        )
    )
    assert isinstance(result.data, dict)
    payments = result.data["data"]
    assert isinstance(payments, list) and len(payments) == 2
    duplicate = [payment for payment in payments if payment.get("duplicate_of")]
    assert len(duplicate) == 1
    assert duplicate[0]["id"] == objective.duplicate_payment_id
    assert duplicate[0]["duplicate_of"] == objective.original_payment_id


def test_generated_scenario_is_solvable_with_safe_retry_after_lost_response() -> None:
    world = payments_world()
    scenario = generate_scenarios(world, count=1, seed=17, response_lost=True)[0]
    sim = Simulator(world, scenario.initial_state, scenario.failures)
    original_before = scenario.initial_state.resources["payment"][
        scenario.objective.original_payment_id
    ]
    refund = AgentAction(
        tool="create_refund",
        arguments={
            "payment_id": scenario.objective.duplicate_payment_id,
            "amount": scenario.objective.amount,
            "idempotency_key": scenario.scenario_id,
        },
    )
    assert sim.execute(refund).status == 504
    assert sim.execute(refund).status == 201
    state = sim.state
    assert state.resources["payment"][scenario.objective.original_payment_id] == original_before
    assert (
        state.resources["payment"][scenario.objective.duplicate_payment_id]["status"] == "refunded"
    )
    assert len(state.resources["refund"]) == 1
    only_refund = next(iter(state.resources["refund"].values()))
    assert only_refund["amount"] == scenario.objective.amount
    assert only_refund["payment_id"] == scenario.objective.duplicate_payment_id
    assert scenario.initial_state.resources["refund"] == {}


def test_compiled_sample_world_generates_and_executes_a_duplicate_refund() -> None:
    sample = Path(__file__).resolve().parents[1] / "examples" / "payments" / "openapi.yaml"
    world = compile_openapi(sample)
    scenario = generate_scenarios(world, count=1, seed=29)[0]
    sim = Simulator(world, scenario.initial_state)
    listed = sim.execute(
        AgentAction(tool="list_payments", arguments={"customer_id": scenario.objective.customer_id})
    )
    assert listed.status == 200 and isinstance(listed.data, dict)
    duplicate = next(payment for payment in listed.data["data"] if payment.get("duplicate_of"))
    result = sim.execute(
        AgentAction(
            tool="create_refund",
            arguments={
                "payment_id": duplicate["id"],
                "amount": duplicate["amount"],
                "idempotency_key": scenario.scenario_id,
            },
        )
    )
    assert result.status == 201
    assert len(sim.state.resources["refund"]) == 1
    assert (
        sim.state.resources["payment"][scenario.objective.original_payment_id]
        == (scenario.initial_state.resources["payment"][scenario.objective.original_payment_id])
    )


def test_scenarios_do_not_share_mutable_state() -> None:
    scenarios = generate_scenarios(payments_world(), count=2, seed=1)
    first, second = scenarios
    first.initial_state.resources["customer"].clear()
    assert len(second.initial_state.resources["customer"]) == 1
    regenerated = generate_scenarios(payments_world(), count=2, seed=1)
    assert len(regenerated[0].initial_state.resources["customer"]) == 1


@pytest.mark.parametrize("count", [-1, 1001, True, 1.5])
def test_invalid_generation_budgets_are_rejected(count: int) -> None:
    with pytest.raises(ValueError, match="scenario count"):
        generate_scenarios(payments_world(), count=count)


def test_empty_generation_and_unannotated_world_failure() -> None:
    assert generate_scenarios(payments_world(), count=0) == []
    world = WorldDefinition(world_id="empty", title="Empty", resources={}, actions={})
    with pytest.raises(ValueError, match="customer/payment/refund"):
        generate_scenarios(world)
    world = payments_world()
    world.rules.clear()
    with pytest.raises(ValueError, match="declared refund rules"):
        generate_scenarios(world)


def test_incompatible_payments_schema_has_a_clear_error() -> None:
    world = payments_world()
    properties = world.resources["payment"].schema_["properties"]
    assert isinstance(properties, dict)
    del properties["duplicate_of"]
    with pytest.raises(ValueError, match="payments sample resource fields"):
        generate_scenarios(world)
