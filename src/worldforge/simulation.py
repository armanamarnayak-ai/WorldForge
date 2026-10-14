"""Deterministic, transactional execution of the supported synthetic world actions.

Every call advances the logical step, even when it fails. Failure occurrence numbers
count calls that pass input and proposed-state validation, including idempotent retries;
invalid requests do not consume an injection. A lost response commits the validated
effect, while an error before execution commits only the step.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from pydantic import JsonValue
from referencing import Registry
from referencing.exceptions import Unresolvable

from worldforge.models import (
    AgentAction,
    FailureInjection,
    ToolResult,
    WorldAction,
    WorldDefinition,
    WorldState,
)

REFUND_RULES = frozenset(
    {
        "refund_payment_exists",
        "refund_amount_positive",
        "refund_total_within_payment",
        "refund_idempotency_key",
    }
)


class _Rejected(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _properties(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    properties = schema.get("properties", {})
    return properties if isinstance(properties, dict) else {}


def _integer(value: JsonValue, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Rejected(400, f"{label} must be an integer in minor currency units")
    return value


def _check_references(value: JsonValue) -> None:
    if not isinstance(value, dict):
        return
    forbidden = {
        "$dynamicRef",
        "$recursiveRef",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$recursiveAnchor",
        "$vocabulary",
        "pattern",
        "patternProperties",
    }
    for key in sorted(forbidden.intersection(value)):
        raise ValueError(f"unsupported schema reference/constraint keyword: {key}")
    # Compiled IR already expands local acyclic references. Requiring resolved IR
    # also excludes recursive local references from hand-authored worlds.
    if "$ref" in value:
        raise ValueError("world schema references must be resolved before simulation")
    if "$schema" in value and value["$schema"] != ("https://json-schema.org/draft/2020-12/schema"):
        raise ValueError("only the Draft 2020-12 schema dialect is supported")
    # Visit schema positions only. Properties may legitimately be called pattern,
    # and defaults/examples are data rather than executable schema constraints.
    for key in ("properties", "$defs", "definitions", "dependentSchemas", "dependencies"):
        mapping = value.get(key)
        if isinstance(mapping, dict):
            for child in mapping.values():
                _check_references(child)
    for key in (
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    ):
        child = value.get(key)
        if isinstance(child, list):
            for item in child:
                _check_references(item)
        else:
            _check_references(child)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        array = value.get(key)
        if isinstance(array, list):
            for child in array:
                _check_references(child)


class Simulator:
    """A per-run in-memory simulator with no network or process execution capabilities."""

    def __init__(
        self,
        world: WorldDefinition,
        initial_state: WorldState | None = None,
        failures: list[FailureInjection] | None = None,
    ) -> None:
        self.world = world.model_copy(deep=True)
        self._validators: dict[str, Validator] = {}
        self._inputs: dict[str, Validator] = {}
        self._responses: dict[str, dict[str, Validator]] = {}
        # An empty referencing Registry defaults to raising NoSuchResource; unlike
        # jsonschema's default registry it never retrieves external schema documents.
        registry: Registry[bool | Mapping[str, Any]] = Registry()
        try:
            for name, resource in self.world.resources.items():
                _check_references(resource.schema_)
                validator_class = validator_for(resource.schema_)
                validator_class.check_schema(resource.schema_)
                self._validators[name] = validator_class(resource.schema_, registry=registry)
            for name, action in self.world.actions.items():
                _check_references(action.input_schema)
                validator_class = validator_for(action.input_schema)
                validator_class.check_schema(action.input_schema)
                self._inputs[name] = validator_class(action.input_schema, registry=registry)
                self._responses[name] = {}
                for status, schema in action.response_schemas.items():
                    _check_references(schema)
                    validator_class = validator_for(schema)
                    validator_class.check_schema(schema)
                    self._responses[name][status] = validator_class(schema, registry=registry)
        except SchemaError as error:
            raise ValueError(f"invalid world JSON schema: {error.message}") from error
        for action in self.world.actions.values():
            if action.supported and action.kind == "refund":
                if not REFUND_RULES.issubset(self.world.rules):
                    raise ValueError("refund actions require all four declared refund rules")
                if action.resource != "refund" or "payment" not in self.world.resources:
                    raise ValueError("refund actions require payment and refund resources")
        self._state = (
            initial_state.model_copy(deep=True) if initial_state is not None else WorldState()
        )
        for name in self.world.resources:
            self._state.resources.setdefault(name, {})
        try:
            self._validate_state(self._state)
        except _Rejected as error:
            raise ValueError(f"invalid initial state: {error.message}") from error
        except Unresolvable as error:
            raise ValueError(
                "initial state schema reference could not be resolved locally"
            ) from error
        self._failures = [item.model_copy(deep=True) for item in failures or []]
        positions: set[tuple[str, int]] = set()
        for failure in self._failures:
            if failure.action not in self.world.actions:
                raise ValueError(f"failure references unknown action: {failure.action}")
            position = (failure.action, failure.occurrence)
            if position in positions:
                raise ValueError("only one failure may target an action occurrence")
            positions.add(position)
        self._occurrences: dict[str, int] = {}
        self.last_failure: FailureInjection | None = None

    @property
    def state(self) -> WorldState:
        """Return a snapshot; caller edits cannot alter subsequent transitions."""
        return self._state.model_copy(deep=True)

    def execute(self, action: AgentAction) -> ToolResult:
        self.last_failure = None
        next_state = self._state.model_copy(deep=True)
        next_state.step += 1
        # The step belongs to the attempt, independent of whether effects are committed.
        self._state.step = next_state.step
        try:
            definition = self.world.actions.get(action.tool)
            if definition is None:
                raise _Rejected(404, f"unknown action: {action.tool}")
            if not definition.supported or definition.kind == "unsupported":
                raise _Rejected(501, definition.reason or "action is not supported")
            errors = list(self._inputs[action.tool].iter_errors(action.arguments))
            if errors:
                raise _Rejected(400, f"invalid arguments: {errors[0].message}")
            result = self._apply(next_state, definition, deepcopy(action.arguments))
            schemas = self._responses[action.tool]
            response_validator = (
                schemas.get(str(result.status))
                or schemas.get(f"{result.status // 100}XX")
                or schemas.get("default")
            )
            if response_validator is not None:
                errors = list(response_validator.iter_errors(result.data))
                if errors:
                    raise _Rejected(422, f"invalid response: {errors[0].message}")
            self._validate_state(next_state)
        except _Rejected as error:
            return ToolResult(status=error.status, error=error.message)
        except Unresolvable:
            return ToolResult(status=422, error="schema reference could not be resolved locally")
        occurrence = self._occurrences.get(action.tool, 0) + 1
        self._occurrences[action.tool] = occurrence
        failure = next(
            (
                item
                for item in self._failures
                if item.action == action.tool and item.occurrence == occurrence
            ),
            None,
        )
        if failure is not None:
            self.last_failure = failure.model_copy(deep=True)
            if failure.kind == "error_before":
                return ToolResult(status=503, error="injected failure before execution")
        self._state = next_state
        if failure is not None:
            return ToolResult(status=504, error="response lost after execution")
        return result.model_copy(deep=True)

    def _apply(
        self, state: WorldState, action: WorldAction, arguments: dict[str, JsonValue]
    ) -> ToolResult:
        resource = action.resource
        if resource is None:
            raise _Rejected(501, "action has no executable resource")
        records = state.resources[resource]
        if action.kind == "get":
            identifier = arguments.get("id")
            if not isinstance(identifier, str):
                raise _Rejected(400, "get requires a string id")
            if identifier not in records:
                raise _Rejected(404, f"{resource} not found")
            return ToolResult(status=200, data=deepcopy(records[identifier]))
        if action.kind == "list":
            matches: list[JsonValue] = [
                deepcopy(record)
                for _, record in sorted(records.items())
                if all(
                    record.get(key) == arguments[key]
                    for key in ("customer_id", "payment_id")
                    if key in arguments
                )
            ]
            return ToolResult(status=200, data={"data": matches})
        if action.kind == "refund":
            return self._refund(state, arguments)
        if action.kind == "create":
            record = self._defaults(resource, arguments)
            identifier = record.get("id")
            if identifier is None:
                identifier = self._identifier(resource, records)
                record["id"] = identifier
            if not isinstance(identifier, str) or not identifier:
                raise _Rejected(400, "resource id must be a nonempty string")
            if identifier in records:
                raise _Rejected(409, f"{resource} id already exists")
            records[identifier] = record
            return ToolResult(status=201, data=deepcopy(record))
        raise _Rejected(501, "action kind is not supported")

    def _defaults(self, resource: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        record = deepcopy(arguments)
        for name, schema in _properties(self.world.resources[resource].schema_).items():
            if name not in record and isinstance(schema, dict) and "default" in schema:
                record[name] = deepcopy(schema["default"])
        return record

    @staticmethod
    def _identifier(resource: str, records: dict[str, dict[str, JsonValue]]) -> str:
        number = 1
        while f"{resource}_{number:06d}" in records:
            number += 1
        return f"{resource}_{number:06d}"

    def _refund(self, state: WorldState, arguments: dict[str, JsonValue]) -> ToolResult:
        payment_id = arguments.get("payment_id")
        if not isinstance(payment_id, str):
            raise _Rejected(400, "refund requires a string payment_id")
        amount = _integer(arguments.get("amount"), "refund amount")
        key = arguments.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            raise _Rejected(400, "refund requires a nonempty idempotency_key")
        refunds = state.resources["refund"]
        for refund in refunds.values():
            if refund.get("idempotency_key") == key:
                if refund.get("payment_id") != payment_id or refund.get("amount") != amount:
                    raise _Rejected(409, "idempotency key conflicts with an existing refund")
                return ToolResult(status=201, data=deepcopy(refund))
        payment = state.resources["payment"].get(payment_id)
        if payment is None:
            raise _Rejected(404, "payment not found")
        if amount <= 0:
            raise _Rejected(400, "refund amount must be positive")
        total = sum(
            _integer(refund.get("amount"), "refund amount")
            for refund in refunds.values()
            if refund.get("payment_id") == payment_id
        )
        payment_amount = _integer(payment.get("amount"), "payment amount")
        if total + amount > payment_amount:
            raise _Rejected(409, "refund amount exceeds remaining payment balance")
        identifier = self._identifier("refund", refunds)
        refund = self._defaults("refund", {**arguments, "id": identifier})
        refunds[identifier] = refund
        properties = _properties(self.world.resources["payment"].schema_)
        if "refunded_amount" in properties:
            payment["refunded_amount"] = total + amount
        if "status" in properties:
            payment["status"] = (
                "refunded" if total + amount == payment_amount else "partially_refunded"
            )
        return ToolResult(status=201, data=deepcopy(refund))

    def _validate_state(self, state: WorldState) -> None:
        unknown = set(state.resources) - set(self.world.resources)
        if unknown:
            raise _Rejected(422, f"unknown state resources: {', '.join(sorted(unknown))}")
        for resource, records in state.resources.items():
            for identifier, record in records.items():
                if not identifier or record.get("id") != identifier:
                    raise _Rejected(422, f"{resource} identifier does not match record id")
                errors = list(self._validators[resource].iter_errors(record))
                if errors:
                    raise _Rejected(422, f"invalid {resource} resource: {errors[0].message}")
        for relation in self.world.relationships:
            targets = state.resources[relation.target_resource]
            for record in state.resources[relation.resource].values():
                target = record.get(relation.field)
                if target is not None and (not isinstance(target, str) or target not in targets):
                    raise _Rejected(422, f"{relation.resource}.{relation.field} target not found")
        if REFUND_RULES.issubset(self.world.rules):
            self._validate_refunds(state)

    @staticmethod
    def _validate_refunds(state: WorldState) -> None:
        payments = state.resources.get("payment", {})
        totals: dict[str, int] = {}
        keys: set[str] = set()
        for refund in state.resources.get("refund", {}).values():
            payment_id = refund.get("payment_id")
            if not isinstance(payment_id, str) or payment_id not in payments:
                raise _Rejected(422, "refund references missing payment")
            amount = _integer(refund.get("amount"), "refund amount")
            if amount <= 0:
                raise _Rejected(422, "refund amount must be positive")
            key = refund.get("idempotency_key")
            if not isinstance(key, str) or not key.strip() or key in keys:
                raise _Rejected(422, "refund idempotency keys must be nonempty and unique")
            keys.add(key)
            totals[payment_id] = totals.get(payment_id, 0) + amount
        for identifier, payment in payments.items():
            amount = _integer(payment.get("amount"), "payment amount")
            total = totals.get(identifier, 0)
            if total > amount:
                raise _Rejected(422, "refund total exceeds payment amount")
            if "refunded_amount" in payment:
                refunded_amount = _integer(payment["refunded_amount"], "refunded_amount")
                if refunded_amount != total:
                    raise _Rejected(422, "payment refunded_amount does not match refund ledger")
