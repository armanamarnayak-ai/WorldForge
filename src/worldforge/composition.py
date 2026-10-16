"""Explicit namespaces and shared deterministic state for small composed worlds.

Composition does not infer cross-service business policies. Relationships are
explicit contracts, and each service keeps its own namespaced resource tables.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import cast

from pydantic import JsonValue, ValidationError

from worldforge.compiler import (
    REFUND_RULES,
    CompilerError,
    _check_compiled_size,
    _check_tree,
)
from worldforge.models import (
    CompatibilityIssue,
    Relationship,
    Scenario,
    WorldDefinition,
    WorldState,
)
from worldforge.simulation import Simulator

MAX_SERVICES = 8
MAX_RELATIONSHIPS = 512
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SCHEMA_MAPS = {"properties", "$defs", "definitions", "dependentSchemas", "dependencies"}
_SCHEMA_ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_CHILDREN = {
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
}


def _namespace_schema(
    schema: dict[str, JsonValue], namespace: str, resource_names: set[str]
) -> dict[str, JsonValue]:
    """Remap annotations in schema positions, preserving literal defaults/examples."""
    result = deepcopy(schema)
    target = result.get("x-worldforge-reference")
    if target is not None:
        if not isinstance(target, str) or target not in resource_names:
            raise CompilerError(f"schema annotation references unknown resource {target!r}")
        result["x-worldforge-reference"] = f"{namespace}.{target}"
    for key in _SCHEMA_MAPS:
        mapping = result.get(key)
        if isinstance(mapping, dict):
            for name, child in mapping.items():
                if isinstance(child, dict):
                    mapping[name] = _namespace_schema(child, namespace, resource_names)
    for key in _SCHEMA_CHILDREN:
        child = result.get(key)
        if isinstance(child, dict):
            result[key] = _namespace_schema(child, namespace, resource_names)
    for key in _SCHEMA_ARRAYS:
        array = result.get(key)
        if isinstance(array, list):
            result[key] = [
                _namespace_schema(child, namespace, resource_names)
                if isinstance(child, dict)
                else child
                for child in array
            ]
    return result


def _validate_relationship(world: WorldDefinition, relationship: Relationship) -> None:
    if (
        relationship.resource not in world.resources
        or relationship.target_resource not in world.resources
    ):
        raise CompilerError("cross-service relationship references an unknown namespaced resource")
    source = world.resources[relationship.resource].schema_.get("properties", {})
    target = world.resources[relationship.target_resource].schema_.get("properties", {})
    field = source.get(relationship.field) if isinstance(source, dict) else None
    identifier = target.get("id") if isinstance(target, dict) else None
    if not isinstance(field, dict) or not isinstance(identifier, dict):
        raise CompilerError("relationship requires an explicit source field and target id schema")
    field_type = field.get("type")
    source_is_string = field_type == "string" or (
        isinstance(field_type, list)
        and "string" in field_type
        and all(isinstance(item, str) and item in {"string", "null"} for item in field_type)
    )
    if not source_is_string or identifier.get("type") != "string":
        raise CompilerError(
            "relationship source field and target id must have compatible string types"
        )


def compose_worlds(
    services: dict[str, WorldDefinition],
    world_id: str = "composed",
    version: str = "1",
    relationships: list[Relationship] | None = None,
) -> WorldDefinition:
    """Compose at most eight local worlds; explicit relationships use qualified IDs.

    The output uses World IR 1.1 because namespaced refund semantics and ownership
    are new. Source-world provenance is retained verbatim, rather than presented as
    a new declaration. ``relationships`` may connect existing service resources;
    no schema or relationship is inferred from similar names.
    """
    if not services or len(services) > MAX_SERVICES:
        raise CompilerError(f"composition requires between 1 and {MAX_SERVICES} services")
    if relationships is not None and len(relationships) > MAX_RELATIONSHIPS:
        raise CompilerError(f"explicit composition relationship count exceeds {MAX_RELATIONSHIPS}")
    if not isinstance(world_id, str) or not world_id or not isinstance(version, str) or not version:
        raise CompilerError("composed world id and version must be nonempty strings")
    _check_tree(
        {
            "world_id": world_id,
            "version": version,
            "relationships": [item.model_dump(mode="json") for item in relationships or []],
        }
    )
    for namespace, source in services.items():
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
            raise CompilerError(
                "service namespace must be lowercase letters/digits/underscores, 1–32 characters"
            )
        _check_tree(source.model_dump(mode="json", by_alias=True))
    resources = {}
    actions = {}
    composed_relationships: list[Relationship] = []
    issues: list[CompatibilityIssue] = []
    source_metadata: dict[str, JsonValue] = {}
    ownership: dict[str, JsonValue] = {}
    tool_metadata: dict[str, JsonValue] = {}
    rules: set[str] = set()
    for namespace, source in sorted(services.items()):
        resource_names = set(source.resources)
        source_metadata[namespace] = {
            "world_id": source.world_id,
            "version": source.version,
            "schema_version": source.schema_version,
            "world": source.model_dump(mode="json", by_alias=True),
        }
        rules.update(source.rules)
        for name, resource in source.resources.items():
            qualified = f"{namespace}.{name}"
            if qualified in resources:
                raise CompilerError(f"composed resource collision: {qualified}")
            resources[qualified] = resource.model_copy(
                update={
                    "name": qualified,
                    "schema_": _namespace_schema(resource.schema_, namespace, resource_names),
                },
                deep=True,
            )
            ownership[qualified] = namespace
        for name, action in source.actions.items():
            if (
                action.supported
                and action.kind == "refund"
                and not REFUND_RULES.issubset(source.rules)
            ):
                raise CompilerError(
                    "each refund service must independently declare all refund rules"
                )
            qualified = f"{namespace}.{name}"
            if qualified in actions:
                raise CompilerError(f"composed action collision: {qualified}")
            actions[qualified] = action.model_copy(
                update={
                    "operation_id": qualified,
                    "resource": f"{namespace}.{action.resource}"
                    if action.resource is not None
                    else None,
                    "path": f"/services/{namespace}{action.path}"
                    if action.path.startswith("/")
                    else f"{namespace}:{action.path}",
                    "input_schema": _namespace_schema(
                        action.input_schema, namespace, resource_names
                    ),
                    "response_schemas": {
                        status: _namespace_schema(schema, namespace, resource_names)
                        for status, schema in action.response_schemas.items()
                    },
                },
                deep=True,
            )
        for relationship in source.relationships:
            composed_relationships.append(
                relationship.model_copy(
                    update={
                        "resource": f"{namespace}.{relationship.resource}",
                        "target_resource": f"{namespace}.{relationship.target_resource}",
                    },
                    deep=True,
                )
            )
        issues.extend(
            item.model_copy(update={"location": f"/services/{namespace}{item.location}"}, deep=True)
            for item in source.issues
        )
        raw_tools = source.metadata.get("mcp_tools")
        if isinstance(raw_tools, dict):
            tool_metadata.update(
                {f"{namespace}.{name}": deepcopy(value) for name, value in raw_tools.items()}
            )
    composed_relationships.extend(item.model_copy(deep=True) for item in relationships or [])
    identity = [
        (item.resource, item.field, item.target_resource) for item in composed_relationships
    ]
    if len(identity) != len(set(identity)):
        raise CompilerError("duplicate composed relationship")
    metadata: dict[str, JsonValue] = {
        "composition": {"services": source_metadata, "ownership": ownership},
    }
    if tool_metadata:
        metadata["mcp_tools"] = tool_metadata
    try:
        world = WorldDefinition(
            schema_version="1.1",
            world_id=world_id,
            version=version,
            title=" + ".join(source.title for _, source in sorted(services.items())),
            resources=resources,
            actions=actions,
            relationships=composed_relationships,
            rules=sorted(rules),
            issues=issues,
            metadata=metadata,
        )
    except ValidationError as exc:
        raise CompilerError(f"invalid composed world: {exc}") from exc
    for relationship in world.relationships:
        _validate_relationship(world, relationship)
    for relationship in relationships or []:
        resource = world.resources[relationship.resource]
        properties = cast(dict[str, JsonValue], resource.schema_["properties"])
        field = cast(dict[str, JsonValue], properties[relationship.field])
        annotation = field.get("x-worldforge-reference")
        if annotation is not None and annotation != relationship.target_resource:
            raise CompilerError(
                "cross-service relationship conflicts with a declared schema annotation"
            )
        field["x-worldforge-reference"] = relationship.target_resource
    _check_compiled_size(world)
    # This validates executable schema constraints, ownership, and refund bindings
    # through the same offline simulator used by actual runs, without executing tools.
    try:
        Simulator(world)
    except ValueError as exc:
        raise CompilerError(f"invalid composed runtime contract: {exc}") from exc
    return world


def service_world(composed: WorldDefinition, namespace: str) -> WorldDefinition:
    """Return a validated copy of the embedded source world, for scenario generation."""
    composition = composed.metadata.get("composition")
    services = composition.get("services") if isinstance(composition, dict) else None
    source = services.get(namespace) if isinstance(services, dict) else None
    if not isinstance(source, dict) or not isinstance(source.get("world"), dict):
        raise CompilerError("composed world does not contain the selected service definition")
    _check_tree(source["world"])
    try:
        world = WorldDefinition.model_validate(source["world"])
    except ValidationError as exc:
        raise CompilerError(f"invalid embedded service world: {exc}") from exc
    if (
        world.world_id != source.get("world_id")
        or world.version != source.get("version")
        or world.schema_version != source.get("schema_version")
    ):
        raise CompilerError("embedded service identity does not match its composition manifest")
    for name in world.resources:
        if f"{namespace}.{name}" not in composed.resources:
            raise CompilerError("embedded service resources do not match the composed world")
    for name in world.actions:
        if f"{namespace}.{name}" not in composed.actions:
            raise CompilerError("embedded service actions do not match the composed world")
    return world


def compose_scenario(
    scenario: Scenario,
    composed: WorldDefinition,
    namespace: str,
    extra_state: WorldState | None = None,
) -> Scenario:
    """Bind a source payments scenario to one service and merge explicit extra state.

    Record identifiers and objective identifiers stay unchanged within their owned
    tables. ``extra_state`` already uses fully qualified resource names; it cannot
    overwrite the source service or supply an independently advanced logical step.
    """
    composition = composed.metadata.get("composition")
    services = composition.get("services") if isinstance(composition, dict) else None
    source = services.get(namespace) if isinstance(services, dict) else None
    if not isinstance(source, dict) or (
        source.get("world_id") != scenario.world_id
        or source.get("version") != scenario.world_version
    ):
        raise CompilerError("scenario must match the selected service world and version")
    state = WorldState(
        resources={name: {} for name in composed.resources}, step=scenario.initial_state.step
    )
    for name, records in scenario.initial_state.resources.items():
        qualified = f"{namespace}.{name}"
        if qualified not in state.resources:
            raise CompilerError(f"scenario contains unknown source resource {name!r}")
        state.resources[qualified] = deepcopy(records)
    if extra_state is not None:
        if extra_state.step != 0:
            raise CompilerError(
                "extra state must have step zero; composed runtime owns the shared step"
            )
        for name, records in extra_state.resources.items():
            if name not in state.resources or name.startswith(f"{namespace}."):
                raise CompilerError(
                    "extra state must belong only to other declared service namespaces"
                )
            state.resources[name] = deepcopy(records)
    allowed = [f"{namespace}.{name}" for name in scenario.allowed_actions]
    if any(name not in composed.actions for name in allowed):
        raise CompilerError("scenario allowed actions do not match the source service")
    failures = [
        item.model_copy(update={"action": f"{namespace}.{item.action}"}, deep=True)
        for item in scenario.failures
    ]
    if any(item.action not in composed.actions for item in failures):
        raise CompilerError("scenario failure references an unknown source action")
    try:
        state = Simulator(composed, state, failures).state
        result = Scenario.model_validate(
            {
                **scenario.model_dump(mode="json"),
                "schema_version": "1.1",
                "scenario_id": f"{namespace}.{scenario.scenario_id}",
                "world_id": composed.world_id,
                "world_version": composed.version,
                "visible": {**deepcopy(scenario.visible), "payments_namespace": namespace},
                "initial_state": state.model_dump(mode="json"),
                "allowed_actions": allowed,
                "failures": [item.model_dump(mode="json") for item in failures],
            }
        )
    except (ValueError, ValidationError) as exc:
        raise CompilerError(f"invalid composed scenario: {exc}") from exc
    return result
