"""Deterministic, transactional execution of the supported synthetic world actions.

Every call advances the logical step, even when it fails. Failure occurrence numbers
count calls that pass input and proposed-state validation, including idempotent retries;
invalid requests do not consume an injection. A lost response commits the validated
effect, while a pre-effect fault commits only the step. Latency is virtual and bounded;
the simulator never sleeps. Stale reads use the initial snapshot. Duplicate delivery
executes a second validated transaction without bypassing conservation or idempotency.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
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
    FailureSpec,
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
_SERVICE_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


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


def _check_schema_budgets(schemas: Sequence[dict[str, JsonValue]]) -> None:
    """Bound all schema data before recursive validators see it, including examples.

    These are simulator trust-boundary limits across one world's resource, input, and
    response schemas, rather than a claim that general JSON Schema is sandboxed.
    """
    pending: list[tuple[JsonValue, int]] = [(schema, 0) for schema in schemas]
    nodes = 0
    string_bytes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 200_000:
            raise ValueError("world schema exceeds simulator depth/node budget")
        if isinstance(value, str):
            string_bytes += len(value.encode("utf-8"))
        elif isinstance(value, dict):
            if nodes + len(pending) + len(value) > 200_000:
                raise ValueError("world schema exceeds simulator depth/node budget")
            for key, child in value.items():
                string_bytes += len(key.encode("utf-8"))
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            if nodes + len(pending) + len(value) > 200_000:
                raise ValueError("world schema exceeds simulator depth/node budget")
            pending.extend((child, depth + 1) for child in value)
        if string_bytes > 4 * 1024 * 1024:
            raise ValueError("world schema exceeds simulator string byte budget")


def _validate_composition(world: WorldDefinition, depth: int = 0) -> dict[str, str]:
    """Reject inconsistent ownership before a composed action can touch any table.

    A manifest records declared source bindings, rather than granting an action
    access to any resource merely because its rewritten identifier exists. Tool
    names can contain dots in ordinary MCP worlds; qualified resource names are
    the composition signal when the manifest itself has been removed.
    """
    if depth > 64:
        raise ValueError("composition exceeds the simulator manifest depth budget")
    composition = world.metadata.get("composition")
    if "composition" not in world.metadata:
        if any("." in resource for resource in world.resources):
            raise ValueError("namespaced resources require a composition ownership manifest")
        if (
            REFUND_RULES.issubset(world.rules)
            and "refund" in world.resources
            and "payment" in world.resources
        ):
            return {"refund": "payment"}
        return {}
    if world.schema_version != "1.1" or not isinstance(composition, dict):
        raise ValueError("composition requires a World IR 1.1 ownership manifest")
    if set(composition) != {"services", "ownership"}:
        raise ValueError("composition manifest requires exactly services and ownership")
    services = composition.get("services")
    ownership = composition.get("ownership")
    if not isinstance(services, dict) or not 1 <= len(services) <= 8:
        raise ValueError("composition manifest requires between 1 and 8 services")
    if not isinstance(ownership, dict) or set(ownership) != set(world.resources):
        raise ValueError("composition ownership must cover exactly the world resources")
    expected_resources: set[str] = set()
    expected_actions: set[str] = set()
    expected_rules: set[str] = set()
    refund_pairs: dict[str, str] = {}
    for namespace, manifest in services.items():
        if not _SERVICE_NAMESPACE.fullmatch(namespace) or not isinstance(manifest, dict):
            raise ValueError("composition manifest contains an invalid service namespace")
        source_data = manifest.get("world")
        if not isinstance(source_data, dict):
            raise ValueError("composition service requires its embedded source world")
        try:
            source = WorldDefinition.model_validate(source_data)
        except ValueError as error:
            raise ValueError("composition contains an invalid embedded source world") from error
        if (
            source.world_id != manifest.get("world_id")
            or source.version != manifest.get("version")
            or source.schema_version != manifest.get("schema_version")
        ):
            raise ValueError("composition source identity does not match its manifest")
        expected_rules.update(source.rules)
        for refund, payment in _validate_composition(source, depth + 1).items():
            refund_pairs[f"{namespace}.{refund}"] = f"{namespace}.{payment}"
        for name in source.resources:
            qualified = f"{namespace}.{name}"
            expected_resources.add(qualified)
            if ownership.get(qualified) != namespace:
                raise ValueError("composition resource ownership conflicts with its namespace")
        for name, declared in source.actions.items():
            qualified = f"{namespace}.{name}"
            expected_actions.add(qualified)
            action = world.actions.get(qualified)
            expected_resource = (
                f"{namespace}.{declared.resource}" if declared.resource is not None else None
            )
            if action is None or (
                action.resource != expected_resource
                or action.kind != declared.kind
                or action.supported != declared.supported
                or action.method != declared.method
            ):
                raise ValueError("composition action conflicts with its declared source binding")
            if action.supported and ownership.get(action.resource or "") != namespace:
                raise ValueError("composition action cannot access another service's resource")
            if (
                declared.supported
                and declared.kind == "refund"
                and not REFUND_RULES.issubset(source.rules)
            ):
                raise ValueError("each composition refund service must declare all refund rules")
    if expected_resources != set(world.resources) or expected_actions != set(world.actions):
        raise ValueError("composition resources/actions do not match the declared source worlds")
    if set(world.rules) != expected_rules:
        raise ValueError("composition rules do not match the declared source worlds")
    return refund_pairs


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
        failures: Sequence[FailureInjection | FailureSpec] | None = None,
        *,
        max_simulated_ms: int = 300_000,
    ) -> None:
        if (
            not isinstance(max_simulated_ms, int)
            or isinstance(max_simulated_ms, bool)
            or not 0 <= max_simulated_ms <= 3_600_000
        ):
            raise ValueError("simulated latency budget must be between 0 and 3600000 ms")
        self.max_simulated_ms = max_simulated_ms
        self.world = world.model_copy(deep=True)
        self._refund_pairs = _validate_composition(self.world)
        schemas_to_check = [resource.schema_ for resource in self.world.resources.values()]
        for definition in self.world.actions.values():
            schemas_to_check.append(definition.input_schema)
            schemas_to_check.extend(definition.response_schemas.values())
        _check_schema_budgets(schemas_to_check)
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
                refund_resource = action.resource or ""
                namespace, _, name = refund_resource.rpartition(".")
                payment_resource = f"{namespace}.payment" if namespace else "payment"
                if (
                    name or refund_resource
                ) != "refund" or payment_resource not in self.world.resources:
                    raise ValueError("refund actions require payment and refund resources")
                self._refund_pairs[refund_resource] = payment_resource
        if "refund" in self.world.resources and "payment" in self.world.resources:
            self._refund_pairs.setdefault("refund", "payment")
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
        self._initial_state = self._state.model_copy(deep=True)
        self._failures = [item.model_copy(deep=True) for item in failures or []]
        if len(self._failures) > 32:
            raise ValueError("at most 32 failure conditions are supported")
        positions: dict[tuple[str, int], set[str]] = {}
        for failure in self._failures:
            if failure.action not in self.world.actions:
                raise ValueError(f"failure references unknown action: {failure.action}")
            position = (failure.action, failure.occurrence)
            kinds = positions.setdefault(position, set())
            if failure.kind in kinds or (failure.kind != "delay" and kinds - {"delay"}):
                raise ValueError("only one effect failure may target an action occurrence")
            kinds.add(failure.kind)
            if failure.kind == "stale_read":
                definition = self.world.actions[failure.action]
                if definition.kind not in {"get", "list"}:
                    raise ValueError("stale_read is supported only for get and list actions")
        self._occurrences: dict[str, int] = {}
        self.last_failure: FailureInjection | FailureSpec | None = None
        self.last_failures: list[FailureInjection | FailureSpec] = []
        self.last_failure_details: list[dict[str, JsonValue]] = []
        self.last_delivery_results: list[ToolResult] = []
        self.last_latency_ms = 0
        self.simulated_elapsed_ms = 0

    @property
    def state(self) -> WorldState:
        """Return a snapshot; caller edits cannot alter subsequent transitions."""
        return self._state.model_copy(deep=True)

    def fork(self) -> Simulator:
        """Stage a transaction without advancing this instance's mutable run state.

        Execution treats the compiled world, initial snapshot, failure definitions,
        and validators as read-only, so they can be shared. Callers must not edit
        this shared configuration. A fork is a transaction helper, not a sandbox
        for trusted Python adapters or a general isolation boundary.
        """
        forked = copy(self)
        forked._state = self._state.model_copy(deep=True)
        forked._occurrences = dict(self._occurrences)
        forked.last_failure = None
        forked.last_failures = []
        forked.last_failure_details = []
        forked.last_delivery_results = []
        forked.last_latency_ms = 0
        return forked

    def execute(self, action: AgentAction) -> ToolResult:
        self.last_failure = None
        self.last_failures = []
        self.last_failure_details = []
        self.last_delivery_results = []
        self.last_latency_ms = 0
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
            self._validate_response(action.tool, result)
            self._validate_state(next_state)
        except _Rejected as error:
            return ToolResult(status=error.status, error=error.message)
        except Unresolvable:
            return ToolResult(status=422, error="schema reference could not be resolved locally")
        occurrence = self._occurrences.get(action.tool, 0) + 1
        self._occurrences[action.tool] = occurrence
        self.last_failures = [
            item.model_copy(deep=True)
            for item in self._failures
            if item.action == action.tool and item.occurrence == occurrence
        ]
        failure = next((item for item in self.last_failures if item.kind != "delay"), None)
        self.last_failure = failure
        requested_latency = sum(
            item.latency_ms for item in self.last_failures if isinstance(item, FailureSpec)
        )
        self.last_latency_ms = min(
            requested_latency, self.max_simulated_ms - self.simulated_elapsed_ms
        )
        self.simulated_elapsed_ms += self.last_latency_ms
        self.last_failure_details = [
            {"kind": item.kind, "effect_committed": False} for item in self.last_failures
        ]
        if requested_latency > self.last_latency_ms:
            for details in self.last_failure_details:
                details["latency_budget_exhausted"] = True
            return ToolResult(
                status=504, error="simulated latency budget exhausted before execution"
            )
        if failure is not None and failure.kind in {
            "error_before",
            "timeout",
            "permission_denied",
            "rate_limit",
            "service_unavailable",
        }:
            statuses = {
                "error_before": 503,
                "timeout": 504,
                "permission_denied": 403,
                "rate_limit": 429,
                "service_unavailable": 503,
            }
            return ToolResult(
                status=statuses[failure.kind], error=f"injected {failure.kind} before execution"
            )
        if failure is not None and failure.kind == "duplicate_delivery":
            duplicate_state = next_state.model_copy(deep=True)
            try:
                duplicate = self._apply(duplicate_state, definition, deepcopy(action.arguments))
                self._validate_response(action.tool, duplicate)
                self._validate_state(duplicate_state)
                next_state = duplicate_state
                duplicate_status = duplicate.status
                duplicate_error = duplicate.error
                duplicate_result = duplicate.model_copy(deep=True)
            except _Rejected as error:
                duplicate_status = error.status
                duplicate_error = error.message
                duplicate_result = ToolResult(status=duplicate_status, error=duplicate_error)
            except Unresolvable:
                duplicate_status = 422
                duplicate_error = "schema reference could not be resolved locally"
                duplicate_result = ToolResult(status=duplicate_status, error=duplicate_error)
            self.last_delivery_results = [duplicate_result]
            for details in self.last_failure_details:
                if details["kind"] == "duplicate_delivery":
                    details["duplicate_status"] = duplicate_status
                    details["duplicate_error"] = duplicate_error
                    details["duplicate_effect_committed"] = duplicate_status < 400
        self._state = next_state
        for details in self.last_failure_details:
            details["effect_committed"] = definition.kind not in {"get", "list"}
        if failure is not None:
            if failure.kind == "response_lost":
                return ToolResult(status=504, error="response lost after execution")
            if failure.kind == "malformed_response":
                # Corruption is confined to the observable JSON payload, after validating
                # the original result and transaction. No invalid state is committed.
                return ToolResult(
                    status=result.status, data=None, error="injected malformed observable response"
                )
            if failure.kind == "stale_read":
                try:
                    stale = self._apply(
                        self._initial_state.model_copy(deep=True),
                        definition,
                        deepcopy(action.arguments),
                    )
                    self._validate_response(action.tool, stale)
                    return stale
                except _Rejected as error:
                    return ToolResult(status=error.status, error=error.message)
                except Unresolvable:
                    return ToolResult(
                        status=422, error="schema reference could not be resolved locally"
                    )
        return result.model_copy(deep=True)

    def _validate_response(self, action: str, result: ToolResult) -> None:
        schemas = self._responses[action]
        response_validator = (
            schemas.get(str(result.status))
            or schemas.get(f"{result.status // 100}XX")
            or schemas.get("default")
        )
        if response_validator is not None:
            errors = list(response_validator.iter_errors(result.data))
            if errors:
                raise _Rejected(422, f"invalid response: {errors[0].message}")

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
            return self._refund(state, resource, arguments)
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

    def _refund(
        self, state: WorldState, resource: str, arguments: dict[str, JsonValue]
    ) -> ToolResult:
        payment_id = arguments.get("payment_id")
        if not isinstance(payment_id, str):
            raise _Rejected(400, "refund requires a string payment_id")
        amount = _integer(arguments.get("amount"), "refund amount")
        key = arguments.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            raise _Rejected(400, "refund requires a nonempty idempotency_key")
        payment_resource = self._refund_pairs[resource]
        refunds = state.resources[resource]
        for refund in refunds.values():
            if refund.get("idempotency_key") == key:
                if refund.get("payment_id") != payment_id or refund.get("amount") != amount:
                    raise _Rejected(409, "idempotency key conflicts with an existing refund")
                return ToolResult(status=201, data=deepcopy(refund))
        payment = state.resources[payment_resource].get(payment_id)
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
        identifier = self._identifier(resource, refunds)
        refund = self._defaults(resource, {**arguments, "id": identifier})
        refunds[identifier] = refund
        properties = _properties(self.world.resources[payment_resource].schema_)
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
            for refund_resource, payment_resource in self._refund_pairs.items():
                self._validate_refunds(state, refund_resource, payment_resource)

    @staticmethod
    def _validate_refunds(
        state: WorldState, refund_resource: str = "refund", payment_resource: str = "payment"
    ) -> None:
        payments = state.resources.get(payment_resource, {})
        totals: dict[str, int] = {}
        keys: set[str] = set()
        for refund in state.resources.get(refund_resource, {}).values():
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
