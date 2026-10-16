"""Deterministic fault semantics preserve validation, state ownership, and budgets."""

from __future__ import annotations

from copy import deepcopy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from test_simulation import initial_state, payments_world, refund_action

from worldforge.composition import compose_worlds
from worldforge.models import AgentAction, FailureInjection, FailureSpec, WorldDefinition
from worldforge.simulation import Simulator


@pytest.mark.parametrize(
    "kind,status,committed",
    [
        ("error_before", 503, False),
        ("timeout", 504, False),
        ("permission_denied", 403, False),
        ("rate_limit", 429, False),
        ("service_unavailable", 503, False),
        ("response_lost", 504, True),
    ],
)
def test_typed_faults_are_bounded_atomic_and_retryable(
    kind: str, status: int, committed: bool
) -> None:
    failure = FailureSpec(action="create_refund", kind=kind, latency_ms=73)
    sim = Simulator(payments_world(), initial_state(), [failure])
    result = sim.execute(refund_action())
    assert result.status == status
    assert sim.last_failures == [failure]
    assert sim.last_failure == failure
    assert sim.last_latency_ms == sim.simulated_elapsed_ms == 73
    assert sim.last_failure_details == [{"kind": kind, "effect_committed": committed}]
    assert bool(sim.state.resources["refund"]) == committed
    assert sim.execute(refund_action()).status == 201
    assert len(sim.state.resources["refund"]) == 1
    assert sim.last_failures == [] and sim.last_failure_details == []
    assert sim.last_latency_ms == 0 and sim.simulated_elapsed_ms == 73


def test_delay_is_virtual_and_composes_with_response_loss_in_declared_order() -> None:
    failures = [
        FailureSpec(action="create_refund", kind="delay", latency_ms=50),
        FailureInjection(action="create_refund", kind="response_lost"),
    ]
    sim = Simulator(payments_world(), initial_state(), failures)
    assert sim.execute(refund_action()).status == 504
    assert sim.last_latency_ms == 50
    assert sim.last_failures == failures
    assert len(sim.state.resources["refund"]) == 1
    assert all(item["effect_committed"] for item in sim.last_failure_details)
    assert sim.execute(refund_action()).status == 201
    assert len(sim.state.resources["refund"]) == 1


def test_delay_only_preserves_normal_response_and_clears_legacy_fault_slot() -> None:
    sim = Simulator(
        payments_world(), initial_state(), [FailureSpec(action="create_refund", kind="delay")]
    )
    assert sim.execute(refund_action()).status == 201
    assert sim.last_failure is None
    assert sim.last_failures[0].kind == "delay"


def test_malformed_response_corrupts_only_observable_json_after_validated_effect() -> None:
    world = payments_world()
    world.actions["create_refund"].response_schemas = {"201": world.resources["refund"].schema_}
    sim = Simulator(
        world,
        initial_state(),
        [FailureSpec(action="create_refund", kind="malformed_response")],
    )
    result = sim.execute(refund_action())
    assert result.status == 201 and result.data is None
    assert result.error == "injected malformed observable response"
    assert sim.state.resources["payment"]["pay_test"]["refunded_amount"] == 1000
    assert sim.execute(refund_action()).status == 201
    assert len(sim.state.resources["refund"]) == 1


def test_stale_get_reads_initial_snapshot_without_rewinding_current_state() -> None:
    sim = Simulator(
        payments_world(),
        initial_state(),
        [FailureSpec(action="get_payment", kind="stale_read")],
    )
    assert sim.execute(refund_action()).status == 201
    get = AgentAction(tool="get_payment", arguments={"id": "pay_test"})
    result = sim.execute(get)
    assert isinstance(result.data, dict)
    assert result.data["refunded_amount"] == 0
    assert sim.state.resources["payment"]["pay_test"]["refunded_amount"] == 1000
    result = sim.execute(get)
    assert isinstance(result.data, dict) and result.data["refunded_amount"] == 1000


def test_stale_list_and_missing_new_record_are_honest_initial_snapshot_results() -> None:
    sim = Simulator(
        payments_world(),
        initial_state(),
        [
            FailureSpec(action="list_refunds", kind="stale_read"),
            FailureSpec(action="get_refund", kind="stale_read"),
        ],
    )
    assert sim.execute(refund_action()).status == 201
    assert sim.execute(AgentAction(tool="list_refunds")).data == {"data": []}
    assert (
        sim.execute(AgentAction(tool="get_refund", arguments={"id": "refund_000001"})).status == 404
    )
    assert len(sim.state.resources["refund"]) == 1


def test_duplicate_refund_delivery_preserves_idempotency_and_conservation() -> None:
    sim = Simulator(
        payments_world(),
        initial_state(),
        [FailureSpec(action="create_refund", kind="duplicate_delivery")],
    )
    assert sim.execute(refund_action()).status == 201
    assert len(sim.state.resources["refund"]) == 1
    assert sim.state.resources["payment"]["pay_test"]["refunded_amount"] == 1000
    assert sim.state.step == 1
    assert sim.last_failure_details == [
        {
            "kind": "duplicate_delivery",
            "effect_committed": True,
            "duplicate_status": 201,
            "duplicate_error": None,
            "duplicate_effect_committed": True,
        }
    ]


def test_duplicate_delivery_is_real_for_non_idempotent_synthetic_create() -> None:
    sim = Simulator(
        payments_world(),
        initial_state(),
        [FailureSpec(action="create_customer", kind="duplicate_delivery")],
    )
    result = sim.execute(AgentAction(tool="create_customer", arguments={"name": "Grace"}))
    assert result.status == 201
    assert len(sim.state.resources["customer"]) == 3
    assert isinstance(result.data, dict) and result.data["id"] == "customer_000001"
    assert sim.last_failure_details[0]["duplicate_status"] == 201


def test_second_delivery_rejection_keeps_first_valid_effect_and_observable_diagnostic() -> None:
    world = payments_world()
    properties = world.actions["create_customer"].input_schema["properties"]
    assert isinstance(properties, dict)
    properties["id"] = {"type": "string"}
    sim = Simulator(
        world,
        initial_state(),
        [FailureSpec(action="create_customer", kind="duplicate_delivery")],
    )
    assert (
        sim.execute(
            AgentAction(tool="create_customer", arguments={"name": "Grace", "id": "unique"})
        ).status
        == 201
    )
    assert len(sim.state.resources["customer"]) == 2
    assert sim.last_failure_details[0]["duplicate_status"] == 409
    assert sim.last_failure_details[0]["duplicate_effect_committed"] is False


def test_invalid_inputs_do_not_consume_new_fault_occurrences_or_latency() -> None:
    sim = Simulator(
        payments_world(),
        initial_state(),
        [FailureSpec(action="create_refund", kind="permission_denied", latency_ms=50)],
    )
    assert sim.execute(refund_action(0)).status == 400
    assert sim.execute(refund_action(1001)).status == 409
    assert sim.last_failures == []
    assert sim.simulated_elapsed_ms == 0
    assert sim.execute(refund_action()).status == 403
    assert sim.simulated_elapsed_ms == 50


def test_latency_budget_saturates_without_committing_excess_delay_effect() -> None:
    sim = Simulator(
        payments_world(),
        initial_state(),
        [
            FailureSpec(action="create_refund", kind="delay", occurrence=1, latency_ms=60),
            FailureSpec(action="create_refund", kind="delay", occurrence=2, latency_ms=60),
        ],
        max_simulated_ms=100,
    )
    assert sim.execute(refund_action(100, "first")).status == 201
    assert sim.execute(refund_action(100, "second")).status == 504
    assert sim.last_latency_ms == 40 and sim.simulated_elapsed_ms == 100
    assert sim.last_failure_details[0]["latency_budget_exhausted"] is True
    assert len(sim.state.resources["refund"]) == 1
    assert sim.execute(refund_action(100, "second")).status == 201


@pytest.mark.parametrize("budget", [-1, 3_600_001, True])
def test_invalid_latency_budgets_rejected(budget: int) -> None:
    with pytest.raises(ValueError, match="latency budget"):
        Simulator(payments_world(), max_simulated_ms=budget)


@pytest.mark.parametrize("kind", ["delay", "response_lost"])
def test_repeated_same_kind_at_same_position_is_rejected(kind: str) -> None:
    failure = FailureSpec(action="create_refund", kind=kind)
    with pytest.raises(ValueError, match="only one effect failure"):
        Simulator(payments_world(), initial_state(), [failure, failure])


def test_conflicting_effect_faults_and_mutation_stale_reads_rejected() -> None:
    with pytest.raises(ValueError, match="only one effect failure"):
        Simulator(
            payments_world(),
            initial_state(),
            [
                FailureSpec(action="create_refund", kind="response_lost"),
                FailureSpec(action="create_refund", kind="permission_denied"),
            ],
        )
    with pytest.raises(ValueError, match="only for get and list"):
        Simulator(
            payments_world(),
            initial_state(),
            [FailureSpec(action="create_refund", kind="stale_read")],
        )


def test_failure_conditions_and_fields_are_bounded() -> None:
    with pytest.raises(ValueError, match="at most 32"):
        Simulator(
            payments_world(),
            initial_state(),
            [FailureSpec(action="create_refund", kind="delay", occurrence=i) for i in range(1, 34)],
        )
    for arguments in ({"latency_ms": 60_001}, {"latency_ms": -1}, {"occurrence": 0}):
        with pytest.raises(ValidationError):
            FailureSpec(action="create_refund", kind="timeout", **arguments)


@settings(deadline=None)
@given(
    kinds=st.lists(
        st.sampled_from(["duplicate_delivery", "response_lost", "error_before", "timeout"]),
        min_size=1,
        max_size=15,
    )
)
def test_arbitrary_fault_sequence_preserves_refund_ledger_and_repeats_exactly(
    kinds: list[str],
) -> None:
    failures = [
        FailureSpec(action="create_refund", kind=kind, occurrence=i)
        for i, kind in enumerate(kinds, 1)
    ]
    first = Simulator(payments_world(), initial_state(), failures)
    second = Simulator(payments_world(), initial_state(), deepcopy(failures))
    for i in range(1, len(kinds) + 2):
        action = refund_action(10, f"condition-{i}")
        assert first.execute(action) == second.execute(action)
        assert first.last_failure_details == second.last_failure_details
        assert first.state == second.state
        refunds = first.state.resources["refund"]
        total = sum(refund["amount"] for refund in refunds.values())
        assert first.state.resources["payment"]["pay_test"]["refunded_amount"] == total
        assert total <= 1000


def _namespaced_payments() -> WorldDefinition:
    world = payments_world()
    data = world.model_dump(mode="json")
    data["world_id"] = "composed-payments-test"
    data["schema_version"] = "1.1"
    data["resources"] = {
        f"payments.{name}": {**resource.model_dump(mode="json"), "name": f"payments.{name}"}
        for name, resource in world.resources.items()
    }
    data["actions"] = {
        f"payments.{name}": {
            **action.model_dump(mode="json"),
            "operation_id": f"payments.{name}",
            "resource": f"payments.{action.resource}",
        }
        for name, action in world.actions.items()
    }
    data["relationships"] = [
        {
            **relation.model_dump(mode="json"),
            "resource": f"payments.{relation.resource}",
            "target_resource": f"payments.{relation.target_resource}",
        }
        for relation in world.relationships
    ]
    data["metadata"] = {
        "composition": {
            "services": {
                "payments": {
                    "world_id": world.world_id,
                    "version": world.version,
                    "schema_version": world.schema_version,
                    "world": world.model_dump(mode="json"),
                }
            },
            "ownership": {f"payments.{name}": "payments" for name in world.resources},
        }
    }
    return WorldDefinition.model_validate(data)


def test_namespaced_refund_tables_preserve_the_same_rules_and_idempotency() -> None:
    state = initial_state()
    state.resources = {f"payments.{key}": value for key, value in state.resources.items()}
    sim = Simulator(_namespaced_payments(), state)
    action = refund_action()
    action.tool = "payments.create_refund"
    assert sim.execute(action).status == sim.execute(action).status == 201
    assert sim.state.resources["payments.payment"]["pay_test"]["refunded_amount"] == 1000
    assert len(sim.state.resources["payments.refund"]) == 1
    assert "refund" not in sim.state.resources


def test_namespaced_refund_initial_state_is_still_validated() -> None:
    state = initial_state()
    state.resources["payment"]["pay_test"]["refunded_amount"] = 500
    state.resources = {f"payments.{key}": value for key, value in state.resources.items()}
    with pytest.raises(ValueError, match="does not match refund ledger"):
        Simulator(_namespaced_payments(), state)


@pytest.mark.parametrize("bomb", ["depth", "nodes", "strings"])
def test_direct_world_ir_schema_bombs_fail_before_json_schema_validation(bomb: str) -> None:
    world = payments_world()
    if bomb == "depth":
        schema = {"type": "object"}
        for _ in range(65):
            schema = {"type": "array", "items": schema}
        world.resources["customer"].schema_ = schema
    elif bomb == "nodes":
        world.resources["customer"].schema_ = {"type": "object", "examples": [None] * 200_001}
    else:
        # The bound is cumulative across the world, rather than per schema.
        world.resources["customer"].schema_["description"] = "x" * (2 * 1024 * 1024)
        world.actions["create_customer"].input_schema["description"] = "x" * (2 * 1024 * 1024)
    with pytest.raises(ValueError, match="simulator .*budget"):
        Simulator(world)


def test_unsupported_reference_keywords_in_direct_ir_remain_fail_closed() -> None:
    world = payments_world()
    world.actions["create_refund"].input_schema["$ref"] = "file:///etc/passwd"
    with pytest.raises(ValueError, match="must be resolved before simulation"):
        Simulator(world, initial_state())


def test_roundtripped_cross_service_action_tamper_fails_before_state_can_be_mutated() -> None:
    composed = compose_worlds({"a": payments_world(), "b": payments_world()})
    composed.actions["a.create_refund"].resource = "b.refund"
    # The resource exists, so generic World IR referential validation permits it.
    roundtripped = WorldDefinition.model_validate(composed.model_dump(mode="json"))
    initial = initial_state()
    initial.resources = {
        f"{service}.{name}": deepcopy(records)
        for service in ("a", "b")
        for name, records in initial.resources.items()
    }
    before = initial.model_copy(deep=True)
    with pytest.raises(ValueError, match="composition action.*source binding"):
        Simulator(roundtripped, initial)
    assert initial == before


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_manifest",
        "wrong_schema_version",
        "missing_services",
        "missing_ownership",
        "missing_resource_owner",
        "extra_resource_owner",
        "conflicting_resource_owner",
        "invalid_service_namespace",
        "missing_source_world",
        "source_identity",
        "undeclared_action",
        "source_refund_rules",
        "dropped_composed_rules",
    ],
)
def test_authored_composed_ir_rejects_missing_or_conflicting_ownership(tamper: str) -> None:
    composed = compose_worlds({"a": payments_world(), "b": payments_world()})
    data = composed.model_dump(mode="json")
    manifest = data["metadata"]["composition"]
    if tamper == "missing_manifest":
        del data["metadata"]["composition"]
    elif tamper == "wrong_schema_version":
        data["schema_version"] = "1.0"
    elif tamper == "missing_services":
        del manifest["services"]
    elif tamper == "missing_ownership":
        del manifest["ownership"]
    elif tamper == "missing_resource_owner":
        del manifest["ownership"]["a.refund"]
    elif tamper == "extra_resource_owner":
        manifest["ownership"]["a.unreported"] = "a"
    elif tamper == "conflicting_resource_owner":
        manifest["ownership"]["a.refund"] = "b"
    elif tamper == "invalid_service_namespace":
        manifest["services"]["../escape"] = manifest["services"].pop("a")
    elif tamper == "missing_source_world":
        del manifest["services"]["a"]["world"]
    elif tamper == "source_identity":
        manifest["services"]["a"]["version"] = "unmatched"
    elif tamper == "undeclared_action":
        data["actions"]["a.unreported"] = {
            **data["actions"]["a.get_payment"],
            "operation_id": "a.unreported",
        }
    elif tamper == "dropped_composed_rules":
        data["rules"] = []
    else:
        # A different service's rules must not silently grant refund semantics.
        manifest["services"]["a"]["world"]["rules"] = []
    roundtripped = WorldDefinition.model_validate(data)
    with pytest.raises(ValueError, match="composition"):
        Simulator(roundtripped)


@pytest.mark.parametrize("declared_rules,expected_status", [(True, 422), (False, 201)])
def test_composed_refund_invariants_follow_each_service_rules_independent_of_action_kind(
    declared_rules: bool, expected_status: int
) -> None:
    source = payments_world()
    source.actions["create_refund"].kind = "create"
    if not declared_rules:
        source.rules = []
    composed = compose_worlds({"a": source, "b": payments_world()})
    state = initial_state()
    state.resources = {f"a.{name}": records for name, records in state.resources.items()}
    simulator = Simulator(composed, state)
    result = simulator.execute(
        AgentAction(
            tool="a.create_refund",
            arguments={"payment_id": "pay_test", "amount": 1001, "idempotency_key": "overrun"},
        )
    )
    assert result.status == expected_status
    assert len(simulator.state.resources["a.refund"]) == (0 if declared_rules else 1)
    assert simulator.state.resources["b.refund"] == {}


def test_composed_declared_refund_rules_validate_initial_state_without_refund_operation() -> None:
    source = payments_world()
    del source.actions["create_refund"]
    composed = compose_worlds({"a": source})
    state = initial_state()
    state.resources["payment"]["pay_test"]["refunded_amount"] = 1001
    state.resources["refund"]["too_much"] = {
        "id": "too_much",
        "payment_id": "pay_test",
        "amount": 1001,
        "idempotency_key": "initial-overrun",
    }
    state.resources = {f"a.{name}": records for name, records in state.resources.items()}
    with pytest.raises(ValueError, match="refund total exceeds payment amount"):
        Simulator(composed, state)


def test_fork_stages_state_occurrences_latency_and_diagnostics_without_affecting_original() -> None:
    simulator = Simulator(
        payments_world(),
        initial_state(),
        [
            FailureSpec(action="create_refund", kind="response_lost", latency_ms=20),
            FailureSpec(action="create_refund", kind="duplicate_delivery", occurrence=2),
        ],
    )
    assert simulator.execute(refund_action(100, "first")).status == 504
    previous_state = simulator.state
    previous_failures = deepcopy(simulator.last_failures)
    previous_details = deepcopy(simulator.last_failure_details)
    forked = simulator.fork()
    assert forked.state == previous_state
    assert forked.simulated_elapsed_ms == 20
    assert forked.last_failures == forked.last_failure_details == forked.last_delivery_results == []
    assert forked.last_failure is None and forked.last_latency_ms == 0
    action = refund_action(100, "second")
    result = forked.execute(action)
    assert result.status == 201 and forked.last_failures[0].kind == "duplicate_delivery"
    assert len(forked.state.resources["refund"]) == 2
    forked.last_failure_details[0]["local_edit"] = True
    forked.last_delivery_results[0].data = "local result edit"
    assert simulator.state == previous_state
    assert simulator.last_failures == previous_failures
    assert simulator.last_failure_details == previous_details
    assert simulator.last_delivery_results == []
    # Its occurrence was not consumed by the discarded speculative fork.
    assert simulator.execute(action) == result
    assert simulator.last_failures[0].kind == "duplicate_delivery"
    assert simulator.state == forked.state
