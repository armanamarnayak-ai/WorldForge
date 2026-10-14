"""Seeded duplicate-payment scenarios for the explicitly declared payments world."""

from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy

from pydantic import JsonValue

from worldforge.models import (
    FailureInjection,
    RefundObjective,
    Scenario,
    WorldDefinition,
    WorldState,
)
from worldforge.simulation import REFUND_RULES, Simulator


def _record(
    world: WorldDefinition, resource: str, values: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    schema = world.resources[resource].schema_
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("payments scenarios require explicit resource property schemas")
    result = {name: deepcopy(value) for name, value in values.items() if name in properties}
    for name, field in properties.items():
        if name not in result and isinstance(field, dict) and "default" in field:
            result[name] = deepcopy(field["default"])
    return result


def generate_scenarios(
    world: WorldDefinition, count: int = 5, seed: int = 0, response_lost: bool = False
) -> list[Scenario]:
    """Generate bounded, reproducible tasks without touching Python's global RNG.

    This release supports the declared customer/payment/refund schema shape only.
    Objective payment IDs remain private; the agent can discover ``duplicate_of``
    through payment listing. Every scenario starts with an empty refund ledger.
    """
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 1000:
        raise ValueError("scenario count must be an integer between 0 and 1000")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("scenario seed must be an integer")
    if not {"customer", "payment", "refund"}.issubset(world.resources):
        raise ValueError("duplicate refund scenarios require customer/payment/refund resources")
    if not REFUND_RULES.issubset(world.rules):
        raise ValueError("duplicate refund scenarios require all declared refund rules")
    payment_properties = world.resources["payment"].schema_.get("properties", {})
    if not isinstance(payment_properties, dict) or not {
        "id",
        "customer_id",
        "amount",
        "currency",
        "status",
        "duplicate_of",
    }.issubset(payment_properties):
        raise ValueError("duplicate refund scenarios require the payments sample resource fields")
    refund_actions = sorted(
        name
        for name, action in world.actions.items()
        if action.supported and action.kind == "refund" and action.resource == "refund"
    )
    payment_lists = [
        action
        for action in world.actions.values()
        if action.supported and action.kind == "list" and action.resource == "payment"
    ]
    if not refund_actions or not payment_lists:
        raise ValueError("duplicate refund scenarios require payment list/refund actions")
    currency = "USD"
    currency_schema = payment_properties["currency"]
    if isinstance(currency_schema, dict):
        choices = currency_schema.get("enum")
        if isinstance(choices, list) and choices and isinstance(choices[0], str):
            currency = choices[0]
        elif isinstance(currency_schema.get("default"), str):
            currency = str(currency_schema["default"])
    rng = random.Random(seed)
    scenarios: list[Scenario] = []
    allowed_actions = sorted(
        name
        for name, action in world.actions.items()
        if action.supported and action.kind != "unsupported"
    )
    for index in range(count):
        customer_id = f"cus_{rng.getrandbits(64):016x}"
        original_id = f"pay_{rng.getrandbits(64):016x}"
        duplicate_id = f"pay_{rng.getrandbits(64):016x}"
        while duplicate_id == original_id:
            duplicate_id = f"pay_{rng.getrandbits(64):016x}"
        amount = rng.randint(100, 50_000)
        customer = _record(
            world,
            "customer",
            {
                "id": customer_id,
                "name": f"Customer {index + 1}",
                "email": f"customer-{index + 1}@example.test",
            },
        )
        common: dict[str, JsonValue] = {
            "customer_id": customer_id,
            "amount": amount,
            "currency": currency,
            "status": "succeeded",
            "refunded_amount": 0,
        }
        original = _record(world, "payment", {**common, "id": original_id, "duplicate_of": None})
        duplicate = _record(
            world, "payment", {**common, "id": duplicate_id, "duplicate_of": original_id}
        )
        state = WorldState(resources={name: {} for name in world.resources})
        state.resources["customer"][customer_id] = customer
        state.resources["payment"][original_id] = original
        state.resources["payment"][duplicate_id] = duplicate
        try:
            state = Simulator(world, state).state
        except ValueError as error:
            message = f"world is incompatible with payments sample scenarios: {error}"
            raise ValueError(message) from error
        identity = json.dumps(
            [world.world_id, world.version, seed, index, response_lost], separators=(",", ":")
        )
        scenario_id = f"SC-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        scenarios.append(
            Scenario(
                scenario_id=scenario_id,
                world_id=world.world_id,
                world_version=world.version,
                seed=seed,
                task=(
                    f"Customer {customer_id} reports a duplicate charge of {amount} minor currency "
                    "units. Investigate the customer's payments and fully refund only "
                    "the duplicate payment. Preserve the original charge and avoid extra refunds."
                ),
                visible={"customer_id": customer_id, "reported_amount": amount},
                initial_state=state,
                objective=RefundObjective(
                    customer_id=customer_id,
                    original_payment_id=original_id,
                    duplicate_payment_id=duplicate_id,
                    amount=amount,
                ),
                allowed_actions=allowed_actions,
                failures=(
                    [FailureInjection(action=refund_actions[0], kind="response_lost", occurrence=1)]
                    if response_lost
                    else []
                ),
            )
        )
    return scenarios
