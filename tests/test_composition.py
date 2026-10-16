from __future__ import annotations

import copy
from pathlib import Path

import pytest

from worldforge.agents import ReferenceAgent
from worldforge.compiler import CompilerError, compile_openapi
from worldforge.composition import compose_scenario, compose_worlds, service_world
from worldforge.mcp import compile_mcp
from worldforge.models import (
    AgentAction,
    AgentDecision,
    AgentObservation,
    Origin,
    Provenance,
    Relationship,
    Scenario,
    WorldDefinition,
    WorldState,
)
from worldforge.runtime import replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios
from worldforge.simulation import Simulator

EXAMPLES = Path(__file__).parents[1] / "examples"


def worlds() -> tuple[WorldDefinition, WorldDefinition]:
    return (
        compile_openapi(EXAMPLES / "payments/openapi.yaml"),
        compile_mcp(EXAMPLES / "email/tools.json"),
    )


def customer_link() -> Relationship:
    return Relationship(
        resource="email.message",
        field="customer_id",
        target_resource="payments.customer",
        provenance=Provenance(
            origin=Origin.DECLARED,
            source="test:composition",
            reason="explicit synthetic email customer ownership",
        ),
    )


def example(response_lost: bool = False) -> tuple[WorldDefinition, Scenario]:
    payments, email = worlds()
    world = compose_worlds({"payments": payments, "email": email}, relationships=[customer_link()])
    scenario = generate_scenarios(payments, count=1, seed=99, response_lost=response_lost)[0]
    extra = WorldState(
        resources={
            "email.message": {
                "message_1": {
                    "id": "message_1",
                    "customer_id": scenario.objective.customer_id,
                    "subject": "Duplicate charge",
                    "body": "Synthetic customer request.",
                }
            }
        }
    )
    return world, compose_scenario(scenario, world, "payments", extra)


def test_composition_namespaces_every_owned_id_and_preserves_provenance() -> None:
    payments, email = worlds()
    before = payments.model_dump(mode="json"), email.model_dump(mode="json")
    world = compose_worlds({"payments": payments, "email": email}, relationships=[customer_link()])
    assert world.schema_version == "1.1"
    assert set(world.resources) == {
        "payments.customer",
        "payments.payment",
        "payments.refund",
        "email.message",
    }
    assert set(world.actions) == {f"payments.{name}" for name in payments.actions} | {
        f"email.{name}" for name in email.actions
    }
    assert world.actions["payments.create_refund"].resource == "payments.refund"
    assert (
        world.resources["payments.payment"].provenance == payments.resources["payment"].provenance
    )
    assert world.actions["email.get_message"].provenance == email.actions["get_message"].provenance
    assert (
        world.resources["payments.payment"].schema_["properties"]["customer_id"][
            "x-worldforge-reference"
        ]
        == "payments.customer"
    )  # type: ignore[index]
    assert (
        world.resources["email.message"].schema_["properties"]["customer_id"][
            "x-worldforge-reference"
        ]
        == "payments.customer"
    )  # type: ignore[index]
    assert world.metadata["mcp_tools"]["email.get_message"]["description"]  # type: ignore[index]
    assert before == (payments.model_dump(mode="json"), email.model_dump(mode="json"))
    assert service_world(world, "payments") == payments
    assert service_world(world, "email") == email


def test_composition_order_is_deterministic_and_copies_are_isolated() -> None:
    payments, email = worlds()
    left = compose_worlds({"payments": payments, "email": email})
    right = compose_worlds({"email": email, "payments": payments})
    assert left == right
    embedded = service_world(left, "payments")
    embedded.metadata["local_edit"] = True
    assert "local_edit" not in service_world(left, "payments").metadata
    left.resources["payments.payment"].schema_["description"] = "local"
    assert "description" not in payments.resources["payment"].schema_


@pytest.mark.parametrize("response_lost", [False, True])
def test_composed_reference_run_preserves_other_service_and_replays(response_lost: bool) -> None:
    world, scenario = example(response_lost)
    before_email = copy.deepcopy(scenario.initial_state.resources["email.message"])
    assert scenario.schema_version == "1.1"
    assert scenario.visible["payments_namespace"] == "payments"
    assert scenario.world_id == world.world_id
    assert all(name.startswith("payments.") for name in scenario.allowed_actions)
    if response_lost:
        assert scenario.failures[0].action == "payments.create_refund"
    run = run_scenario_sync(world, scenario, ReferenceAgent())
    assert run.evaluation.success
    assert len(run.final_state.resources["payments.refund"]) == 1
    assert run.final_state.resources["email.message"] == before_email
    assert replay_run(run)


class EmailThenRefundAgent(ReferenceAgent):
    """Use an observed synthetic email's customer reference before a payment lookup."""

    def __init__(self) -> None:
        super().__init__()
        self._email_requested = False
        self._customer_id: str | None = None

    async def reset(self) -> None:
        await super().reset()
        self._email_requested = False
        self._customer_id = None

    async def act(self, observation: AgentObservation) -> AgentDecision:
        if not self._email_requested:
            self._email_requested = True
            return AgentDecision(
                action=AgentAction(tool="email.get_message", arguments={"id": "message_1"})
            )
        if self._customer_id is None:
            result = observation.last_result
            if (
                result is None
                or not isinstance(result.data, dict)
                or not isinstance(result.data.get("customer_id"), str)
            ):
                return AgentDecision(message="Customer email cannot be read.")
            self._customer_id = result.data["customer_id"]
        bound = observation.model_copy(deep=True)
        bound.visible["customer_id"] = self._customer_id
        return await super().act(bound)


def test_cross_service_agent_reads_email_then_refunds_using_observed_customer() -> None:
    world, scenario = example(response_lost=True)
    del scenario.visible["customer_id"]
    scenario.allowed_actions.append("email.get_message")
    run = run_scenario_sync(world, scenario, EmailThenRefundAgent())
    assert run.evaluation.success
    actions = [
        event.payload["action"]["tool"] for event in run.events if event.type == "TOOL_CALLED"
    ]  # type: ignore[index]
    assert actions == [
        "email.get_message",
        "payments.list_payments",
        "payments.create_refund",
        "payments.create_refund",
    ]
    assert (
        run.final_state.resources["email.message"]
        == scenario.initial_state.resources["email.message"]
    )
    assert replay_run(run)


def test_two_payment_services_have_independent_ledgers_and_idempotency_keys() -> None:
    payments, _ = worlds()
    world = compose_worlds({"payments": payments, "secondary": payments})
    source = generate_scenarios(payments, count=1, seed=77)[0]
    extra = WorldState(
        resources={
            f"secondary.{name}": copy.deepcopy(records)
            for name, records in source.initial_state.resources.items()
        }
    )
    scenario = compose_scenario(source, world, "payments", extra)
    simulator = Simulator(world, scenario.initial_state)
    args = {
        "payment_id": source.objective.duplicate_payment_id,
        "amount": source.objective.amount,
        "idempotency_key": "same-key",
    }
    assert (
        simulator.execute(AgentAction(tool="payments.create_refund", arguments=args)).status == 201
    )
    assert not simulator.state.resources["secondary.refund"]
    assert (
        simulator.execute(AgentAction(tool="secondary.create_refund", arguments=args)).status == 201
    )
    assert len(simulator.state.resources["payments.refund"]) == 1
    assert len(simulator.state.resources["secondary.refund"]) == 1
    assert simulator.state.step == 2


def test_explicit_relationship_enforced_without_cross_service_execution() -> None:
    world, scenario = example()
    simulator = Simulator(world, scenario.initial_state)
    invalid = AgentAction(
        tool="email.create_message",
        arguments={"customer_id": "missing", "subject": "Hi", "body": "Fixture"},
    )
    result = simulator.execute(invalid)
    assert result.status == 422
    assert (
        simulator.state.resources["email.message"]
        == scenario.initial_state.resources["email.message"]
    )
    valid = invalid.model_copy(deep=True)
    valid.arguments["customer_id"] = scenario.objective.customer_id
    assert simulator.execute(valid).status == 201
    assert (
        simulator.state.resources["payments.payment"]
        == scenario.initial_state.resources["payments.payment"]
    )


@pytest.mark.parametrize("namespace", ["../escape", "name.with.dot", "Name", "", "x" * 33])
def test_unsafe_or_ambiguous_namespaces_rejected(namespace: str) -> None:
    payments, _ = worlds()
    with pytest.raises(CompilerError, match="namespace"):
        compose_worlds({namespace: payments})


def test_service_count_empty_id_and_individual_refund_rules_are_bounded() -> None:
    payments, _ = worlds()
    with pytest.raises(CompilerError, match="between 1 and 8"):
        compose_worlds({})
    with pytest.raises(CompilerError, match="between 1 and 8"):
        compose_worlds({f"service_{index}": payments for index in range(9)})
    with pytest.raises(CompilerError, match="nonempty"):
        compose_worlds({"payments": payments}, world_id="")
    unsafe = payments.model_copy(deep=True)
    unsafe.rules = []
    with pytest.raises(CompilerError, match="independently declare"):
        compose_worlds({"payments": unsafe, "secondary": payments})


@pytest.mark.parametrize(
    "condition", ["missing_resource", "missing_field", "wrong_type", "target_id_type", "duplicate"]
)
def test_invalid_cross_service_relationships_rejected(condition: str) -> None:
    payments, email = worlds()
    relationship = customer_link()
    relationships = [relationship]
    if condition == "missing_resource":
        relationship.target_resource = "missing.customer"
    elif condition == "missing_field":
        relationship.field = "missing"
    elif condition == "wrong_type":
        email.resources["message"].schema_["properties"]["customer_id"] = {"type": "integer"}  # type: ignore[index]
    elif condition == "target_id_type":
        payments.resources["customer"].schema_["properties"]["id"] = {"type": "integer"}  # type: ignore[index]
    else:
        relationships.append(relationship.model_copy(deep=True))
    with pytest.raises(CompilerError):
        compose_worlds({"payments": payments, "email": email}, relationships=relationships)


def test_composition_does_not_rewrite_literal_schema_examples() -> None:
    payments, email = worlds()
    literal = {"x-worldforge-reference": "not-a-resource"}
    email.resources["message"].schema_["example"] = literal
    world = compose_worlds({"payments": payments, "email": email})
    assert world.resources["email.message"].schema_["example"] == literal


def test_scenario_binding_rejects_unknown_service_or_version() -> None:
    payments, email = worlds()
    world = compose_worlds({"payments": payments, "email": email})
    scenario = generate_scenarios(payments, count=1)[0]
    with pytest.raises(CompilerError, match="match the selected service"):
        compose_scenario(scenario, world, "email")
    scenario.world_version = "wrong-version"
    with pytest.raises(CompilerError, match="match the selected service"):
        compose_scenario(scenario, world, "payments")


@pytest.mark.parametrize(
    "resource,step", [("payments.payment", 0), ("missing.resource", 0), ("email.message", 1)]
)
def test_extra_state_cannot_overwrite_owned_state_or_shared_steps(resource: str, step: int) -> None:
    payments, email = worlds()
    world = compose_worlds({"payments": payments, "email": email})
    scenario = generate_scenarios(payments, count=1)[0]
    with pytest.raises(CompilerError):
        compose_scenario(
            scenario, world, "payments", WorldState(resources={resource: {}}, step=step)
        )


def test_composed_initial_cross_service_references_are_validated() -> None:
    payments, email = worlds()
    world = compose_worlds({"payments": payments, "email": email}, relationships=[customer_link()])
    scenario = generate_scenarios(payments, count=1)[0]
    extra = WorldState(
        resources={
            "email.message": {
                "bad": {"id": "bad", "customer_id": "missing", "subject": "Hi", "body": "Fixture"}
            }
        }
    )
    with pytest.raises(CompilerError, match="invalid composed scenario"):
        compose_scenario(scenario, world, "payments", extra)


def test_service_world_rejects_missing_or_tampered_manifest() -> None:
    payments, email = worlds()
    world = compose_worlds({"payments": payments, "email": email})
    with pytest.raises(CompilerError, match="selected service"):
        service_world(world, "missing")
    world.metadata["composition"]["services"]["payments"]["version"] = "wrong"  # type: ignore[index]
    with pytest.raises(CompilerError, match="identity"):
        service_world(world, "payments")


@pytest.mark.parametrize(
    "condition", ["action_owner", "resource_owner", "missing_manifest", "missing_owner"]
)
def test_authored_composed_world_cannot_escape_declared_service_ownership(condition: str) -> None:
    payments, _ = worlds()
    world = compose_worlds({"payments": payments, "secondary": payments})
    if condition == "action_owner":
        world.actions["payments.create_refund"].resource = "secondary.refund"
    elif condition == "resource_owner":
        world.metadata["composition"]["ownership"]["payments.payment"] = "secondary"  # type: ignore[index]
    elif condition == "missing_manifest":
        del world.metadata["composition"]
    else:
        del world.metadata["composition"]["ownership"]["payments.payment"]  # type: ignore[index]
    # The general IR contract intentionally allows authored semantic declarations;
    # simulator binding must still enforce the manifest before any effect executes.
    roundtrip = WorldDefinition.model_validate_json(world.model_dump_json())
    with pytest.raises(ValueError, match="composition|ownership|binding"):
        Simulator(roundtrip)


def test_composition_total_artifact_size_is_bounded() -> None:
    payments, email = worlds()
    payments.metadata["large"] = "x" * 900_000
    email.metadata["large"] = "y" * 900_000
    # Each input fits the source bound; repeated complete sources exceed the output bound.
    with pytest.raises(CompilerError, match="serialized output limit"):
        compose_worlds({"a": payments, "b": payments, "c": email, "d": email, "e": email})
