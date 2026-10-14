from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from worldforge.models import (
    AgentAction,
    FailureInjection,
    Origin,
    Provenance,
    Relationship,
    WorldAction,
    WorldDefinition,
    WorldResource,
    WorldState,
)
from worldforge.simulation import REFUND_RULES, Simulator


def payments_world() -> WorldDefinition:
    provenance = Provenance(origin=Origin.DECLARED, source="test", reason="declared test semantics")
    schemas: dict[str, dict[str, JsonValue]] = {
        "customer": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "name"],
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "email": {"type": "string"},
            },
        },
        "payment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "customer_id", "amount", "currency", "status"],
            "properties": {
                "id": {"type": "string"},
                "customer_id": {"type": "string"},
                "amount": {"type": "integer", "minimum": 1},
                "currency": {"type": "string", "enum": ["USD"]},
                "status": {
                    "type": "string",
                    "enum": ["succeeded", "partially_refunded", "refunded"],
                    "default": "succeeded",
                },
                "refunded_amount": {"type": "integer", "minimum": 0, "default": 0},
                "duplicate_of": {"type": ["string", "null"], "default": None},
            },
        },
        "refund": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "payment_id", "amount", "idempotency_key"],
            "properties": {
                "id": {"type": "string"},
                "payment_id": {"type": "string"},
                "amount": {"type": "integer", "minimum": 1},
                "idempotency_key": {"type": "string", "minLength": 1},
            },
        },
    }
    actions: dict[str, WorldAction] = {}
    for resource, schema in schemas.items():
        properties = schema["properties"]
        assert isinstance(properties, dict)
        arguments = deepcopy(properties)
        arguments.pop("id")
        if resource == "payment":
            for key in ("status", "refunded_amount", "duplicate_of"):
                arguments.pop(key)
        required = [key for key in schema["required"] if key in arguments]
        name = f"create_{resource}"
        actions[name] = WorldAction(
            operation_id=name,
            method="POST",
            path=f"/{resource}",
            kind="refund" if resource == "refund" else "create",
            resource=resource,
            input_schema={
                "type": "object",
                "properties": arguments,
                "required": required,
                "additionalProperties": False,
            },
            provenance=provenance,
        )
        name = f"get_{resource}"
        actions[name] = WorldAction(
            operation_id=name,
            method="GET",
            path=f"/{resource}/{{id}}",
            kind="get",
            resource=resource,
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            provenance=provenance,
        )
        name = f"list_{resource}s"
        filters = {"payment": "customer_id", "refund": "payment_id"}
        actions[name] = WorldAction(
            operation_id=name,
            method="GET",
            path=f"/{resource}",
            kind="list",
            resource=resource,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {filters[resource]: {"type": "string"}}
                if resource in filters
                else {},
            },
            provenance=provenance,
        )
    return WorldDefinition(
        world_id="payments-test",
        title="Payments",
        resources={
            name: WorldResource(name=name, schema=schema, provenance=provenance)
            for name, schema in schemas.items()
        },
        actions=actions,
        rules=sorted(REFUND_RULES),
        relationships=[
            Relationship(
                resource="payment",
                field="customer_id",
                target_resource="customer",
                provenance=provenance,
            ),
            Relationship(
                resource="payment",
                field="duplicate_of",
                target_resource="payment",
                provenance=provenance,
            ),
            Relationship(
                resource="refund",
                field="payment_id",
                target_resource="payment",
                provenance=provenance,
            ),
        ],
    )


def initial_state(amount: int = 1000) -> WorldState:
    return WorldState(
        resources={
            "customer": {"cus_test": {"id": "cus_test", "name": "Ada"}},
            "payment": {
                "pay_test": {
                    "id": "pay_test",
                    "customer_id": "cus_test",
                    "amount": amount,
                    "currency": "USD",
                    "status": "succeeded",
                    "refunded_amount": 0,
                }
            },
            "refund": {},
        }
    )


def refund_action(amount: int = 1000, key: str = "retry-safe") -> AgentAction:
    return AgentAction(
        tool="create_refund",
        arguments={
            "payment_id": "pay_test",
            "amount": amount,
            "idempotency_key": key,
        },
    )


def test_refund_transitions_and_conservation() -> None:
    sim = Simulator(payments_world(), initial_state())
    partial = sim.execute(refund_action(300, "part-one"))
    assert partial.status == 201
    assert sim.state.resources["payment"]["pay_test"]["status"] == "partially_refunded"
    assert sim.state.resources["payment"]["pay_test"]["refunded_amount"] == 300
    assert sim.execute(refund_action(700, "part-two")).status == 201
    assert sim.state.resources["payment"]["pay_test"]["status"] == "refunded"
    assert sim.state.resources["payment"]["pay_test"]["refunded_amount"] == 1000
    before = sim.state.resources
    assert sim.execute(refund_action(1, "excess")).status == 409
    assert sim.state.resources == before
    assert sim.state.step == 3


def test_idempotent_retry_is_identical_and_conflicting_payload_rejected() -> None:
    sim = Simulator(payments_world(), initial_state())
    result = sim.execute(refund_action(600))
    before = sim.state.resources
    assert sim.execute(refund_action(600)) == result
    assert sim.execute(refund_action(500)).status == 409
    assert sim.state.resources == before
    assert len(sim.state.resources["refund"]) == 1


@pytest.mark.parametrize(
    "kind,status,committed",
    [
        ("error_before", 503, False),
        ("response_lost", 504, True),
    ],
)
def test_failures_are_atomic_and_retries_safe(kind: str, status: int, committed: bool) -> None:
    failure = FailureInjection(action="create_refund", kind=kind, occurrence=1)
    sim = Simulator(payments_world(), initial_state(), [failure])
    result = sim.execute(refund_action())
    assert result.status == status and result.data is None
    assert sim.last_failure == failure
    assert bool(sim.state.resources["refund"]) is committed
    assert sim.execute(refund_action()).status == 201
    assert sim.last_failure is None
    assert len(sim.state.resources["refund"]) == 1
    assert sim.state.step == 2


def test_invalid_requests_do_not_consume_failure_occurrence() -> None:
    sim = Simulator(
        payments_world(),
        initial_state(),
        [
            FailureInjection(action="create_refund", kind="response_lost", occurrence=1),
        ],
    )
    assert sim.execute(refund_action(0)).status == 400
    assert sim.last_failure is None
    assert sim.execute(refund_action(1001)).status == 409
    assert sim.execute(refund_action()).status == 504
    assert sim.state.step == 3
    assert len(sim.state.resources["refund"]) == 1


def test_every_attempt_advances_step_and_unsupported_actions_fail_closed() -> None:
    world = payments_world()
    world.actions["send_wire"] = WorldAction(
        operation_id="send_wire",
        method="POST",
        path="/wire",
        kind="unsupported",
        supported=False,
        provenance=world.actions["create_refund"].provenance,
    )
    sim = Simulator(world, initial_state())
    assert sim.execute(AgentAction(tool="missing")).status == 404
    assert sim.execute(AgentAction(tool="send_wire")).status == 501
    assert sim.execute(AgentAction(tool="get_payment")).status == 400
    assert sim.execute(AgentAction(tool="get_payment", arguments={"id": "unknown"})).status == 404
    assert sim.state.step == 4
    assert sim.state.resources == initial_state().resources


def test_generic_create_get_and_filtered_list_validate_relationships() -> None:
    sim = Simulator(payments_world(), initial_state())
    bad = AgentAction(
        tool="create_payment",
        arguments={
            "customer_id": "absent",
            "amount": 1500,
            "currency": "USD",
        },
    )
    before = sim.state.resources
    assert sim.execute(bad).status == 422
    assert sim.state.resources == before
    bad.arguments["customer_id"] = "cus_test"
    created = sim.execute(bad)
    assert created.status == 201 and isinstance(created.data, dict)
    assert created.data["status"] == "succeeded"
    assert created.data["refunded_amount"] == 0
    identifier = created.data["id"]
    assert sim.execute(AgentAction(tool="get_payment", arguments={"id": identifier})).data == (
        created.data
    )
    listed = sim.execute(AgentAction(tool="list_payments", arguments={"customer_id": "cus_test"}))
    assert isinstance(listed.data, dict) and len(listed.data["data"]) == 2
    assert sim.execute(
        AgentAction(tool="list_payments", arguments={"customer_id": "other"})
    ).data == {"data": []}


def test_schema_validation_and_results_are_isolated_from_caller_mutations() -> None:
    state = initial_state()
    world = payments_world()
    sim = Simulator(world, state)
    state.resources["payment"]["pay_test"]["amount"] = 1
    world.rules.clear()
    snapshot = sim.state
    snapshot.resources["payment"]["pay_test"]["amount"] = 2
    result = sim.execute(refund_action())
    assert result.status == 201 and isinstance(result.data, dict)
    result.data["amount"] = 900_000
    assert sim.state.resources["refund"]["refund_000001"]["amount"] == 1000
    assert sim.state.resources["payment"]["pay_test"]["amount"] == 1000


@pytest.mark.parametrize("corruption", ["id", "foreign_key", "schema", "overrefund", "ledger"])
def test_invalid_initial_state_is_rejected(corruption: str) -> None:
    state = initial_state()
    payment = state.resources["payment"]["pay_test"]
    if corruption == "id":
        payment["id"] = "different"
    elif corruption == "foreign_key":
        payment["customer_id"] = "missing"
    elif corruption == "schema":
        payment["amount"] = "1000"
    elif corruption == "ledger":
        payment["refunded_amount"] = 1
    else:
        state.resources["refund"]["ref_bad"] = {
            "id": "ref_bad",
            "payment_id": "pay_test",
            "amount": 1001,
            "idempotency_key": "bad",
        }
        payment["refunded_amount"] = 1001
    with pytest.raises(ValueError, match="invalid initial state"):
        Simulator(payments_world(), state)


def test_refund_behavior_requires_explicit_complete_rule_declarations() -> None:
    world = payments_world()
    world.rules.pop()
    with pytest.raises(ValueError, match="declared refund rules"):
        Simulator(world, initial_state())


def test_refunded_amount_is_not_added_when_schema_does_not_declare_it() -> None:
    world = payments_world()
    properties = world.resources["payment"].schema_["properties"]
    assert isinstance(properties, dict)
    del properties["refunded_amount"]
    state = initial_state()
    del state.resources["payment"]["pay_test"]["refunded_amount"]
    sim = Simulator(world, state)
    assert sim.execute(refund_action()).status == 201
    assert "refunded_amount" not in sim.state.resources["payment"]["pay_test"]


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef", "$id"])
def test_remote_reference_keywords_fail_before_any_retrieval(keyword: str) -> None:
    world = payments_world()
    world.resources["customer"].schema_[keyword] = "http://127.0.0.1:1/untrusted"
    with pytest.raises(ValueError, match="reference"):
        Simulator(world, initial_state())


def test_unresolved_local_reference_fails_with_a_controlled_error() -> None:
    world = payments_world()
    world.actions["get_payment"].input_schema = {"$ref": "#/$defs/does_not_exist"}
    with pytest.raises(ValueError, match="references must be resolved"):
        Simulator(world, initial_state())


def test_offline_registry_blocks_retrieval_even_if_schema_precheck_is_bypassed() -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"type":"object"}')

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        sim = Simulator(payments_world(), initial_state())
        # Evolve the validator deliberately to bypass constructor checks and test
        # the independent registry. Evolve preserves the simulator's registry.
        sim._validators["customer"] = sim._validators["customer"].evolve(
            schema={"$ref": f"http://127.0.0.1:{server.server_port}/schema"}
        )
        result = sim.execute(AgentAction(tool="list_customers"))
        assert result.status == 422
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("keyword", ["pattern", "patternProperties"])
def test_regex_keywords_are_explicitly_unsupported(keyword: str) -> None:
    world = payments_world()
    world.resources["customer"].schema_[keyword] = (
        "(a+)+$" if keyword == "pattern" else {"(a+)+$": {"type": "string"}}
    )
    with pytest.raises(ValueError, match="unsupported schema"):
        Simulator(world, initial_state())


def test_literal_properties_and_default_data_named_pattern_are_preserved() -> None:
    world = payments_world()
    schema = world.resources["customer"].schema_
    properties = schema["properties"]
    assert isinstance(properties, dict)
    properties["pattern"] = {"type": "string", "default": "literal value"}
    schema["examples"] = [{"patternProperties": "data", "$ref": "data"}]
    sim = Simulator(world, initial_state())
    result = sim.execute(AgentAction(tool="create_customer", arguments={"name": "Grace"}))
    assert result.status == 201 and isinstance(result.data, dict)
    assert result.data["pattern"] == "literal value"


@pytest.mark.parametrize("status", ["201", "2XX", "default"])
def test_successful_response_schema_mismatch_rolls_back_effects(status: str) -> None:
    world = payments_world()
    world.actions["create_customer"].response_schemas = {status: {"type": "array"}}
    sim = Simulator(world, initial_state())
    before = sim.state.resources
    result = sim.execute(AgentAction(tool="create_customer", arguments={"name": "Grace"}))
    assert result.status == 422 and "invalid response" in (result.error or "")
    assert sim.state.resources == before
    assert sim.state.step == 1


def test_declared_response_schema_is_checked_and_undeclared_status_has_no_check() -> None:
    world = payments_world()
    world.actions["create_refund"].response_schemas = {
        "201": world.resources["refund"].schema_,
        "200": {"type": "array"},
    }
    sim = Simulator(world, initial_state())
    first = sim.execute(refund_action())
    assert first.status == 201
    assert sim.execute(refund_action()) == first
    world.actions["create_refund"].response_schemas = {"200": {"type": "array"}}
    assert Simulator(world, initial_state()).execute(refund_action()).status == 201


def test_response_schema_remote_reference_is_rejected_before_execution() -> None:
    world = payments_world()
    world.actions["get_payment"].response_schemas = {
        "200": {"$ref": "https://untrusted.example/schema"},
    }
    with pytest.raises(ValueError, match="references must be resolved"):
        Simulator(world, initial_state())


@given(st.lists(st.integers(min_value=1, max_value=1200), min_size=0, max_size=30))
def test_arbitrary_refund_sequence_never_exceeds_payment_amount(amounts: list[int]) -> None:
    sim = Simulator(payments_world(), initial_state())
    accepted = 0
    for index, amount in enumerate(amounts):
        result = sim.execute(refund_action(amount, f"request-{index}"))
        if accepted + amount <= 1000:
            assert result.status == 201
            accepted += amount
        else:
            assert result.status == 409
        assert sim.state.resources["payment"]["pay_test"]["refunded_amount"] == accepted
        ledger = sum(refund["amount"] for refund in sim.state.resources["refund"].values())
        assert ledger == accepted <= 1000
    assert sim.state.step == len(amounts)
