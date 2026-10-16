"""Compile bounded local MCP tool declarations, without contacting MCP servers.

Tool schemas describe interfaces, not business behavior. Executable synthetic
behavior requires explicit ``x-worldforge`` resource, action, and rule declarations.
MCP annotations remain descriptive metadata and never grant runtime capabilities.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue, ValidationError

from worldforge.compiler import (
    MAX_SOURCE_BYTES,
    REFUND_RULES,
    CompilerError,
    _check_compiled_size,
    _check_tree,
    _load,
    _mapping,
    _origin,
    _Resolver,
    _schema,
    _text,
)
from worldforge.models import (
    CompatibilityIssue,
    Origin,
    Relationship,
    WorldAction,
    WorldDefinition,
    WorldResource,
)

MAX_TOOLS = 256
MAX_RESOURCES = 128
_RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TOOL_FIELDS = {
    "name",
    "title",
    "description",
    "inputSchema",
    "outputSchema",
    "annotations",
    "_meta",
    "x-worldforge",
}


def compile_mcp(path: Path | str) -> WorldDefinition:
    """Read a regular bounded local JSON/YAML tools document; never fetch references."""
    source = Path(path)
    return compile_mcp_document(_load(source), source=str(source))


def _action_reasons(
    kind: str,
    resource: str | None,
    schema: dict[str, JsonValue],
    resources: dict[str, WorldResource],
    rules: list[str],
) -> list[str]:
    reasons: list[str] = []
    properties = _mapping(schema.get("properties", {}), "inputSchema/properties")
    required = cast(list[str], schema.get("required", []))
    if schema.get("type") != "object":
        reasons.append("inputSchema requires explicit object type")
    if kind in {"get", "list", "refund"} and schema.get("additionalProperties") is not False:
        reasons.append(
            f"{kind} requires additionalProperties: false; extra arguments have no semantics"
        )
    if kind == "get":
        if set(properties) != {"id"} or "id" not in required:
            reasons.append("get implements only a required string argument named id")
        identifier = properties.get("id")
        if not isinstance(identifier, dict) or identifier.get("type") != "string":
            reasons.append("get requires id with string type")
    if kind == "list":
        allowed_filters = {"payment": {"customer_id"}, "refund": {"payment_id"}}
        unknown = set(properties) - allowed_filters.get(resource or "", set())
        if unknown:
            reasons.append("list does not implement filters: " + ", ".join(sorted(unknown)))
        for name, field in properties.items():
            if not isinstance(field, dict) or field.get("type") != "string":
                reasons.append(f"list filter {name} requires string type")
    if kind == "refund":
        missing_rules = REFUND_RULES - set(rules)
        if missing_rules:
            reasons.append(
                "refund requires explicitly declared rules: " + ", ".join(sorted(missing_rules))
            )
        if resource != "refund" or "payment" not in resources:
            reasons.append("refund requires declared payment and refund resources")
        fields = {"payment_id": "string", "amount": "integer", "idempotency_key": "string"}
        if set(properties) != set(fields) or not set(fields).issubset(required):
            reasons.append("refund implements only required payment_id, amount, idempotency_key")
        for name, field_type in fields.items():
            field = properties.get(name)
            if not isinstance(field, dict) or field.get("type") != field_type:
                reasons.append(f"refund {name} requires {field_type} type")
    return reasons


def compile_mcp_document(document: dict[str, Any], source: str = "memory") -> WorldDefinition:
    """Compile the documented static subset into the existing World IR 1.0 contract.

    Supported keys are a ``tools`` array, local acyclic ``$defs`` references, and
    root ``x-worldforge`` declarations: id, version, title, resources, and rules.
    Unknown protocol/tool features are reported; they are never executed.
    """
    _check_tree(document)
    _mapping(document, "document")
    _text(source, "source")
    try:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise CompilerError(f"source name exceeds the {MAX_SOURCE_BYTES} byte limit")
    except UnicodeError as exc:
        raise CompilerError("source name must be valid UTF-8") from exc
    resolved = cast(dict[str, Any], _Resolver(document).resolve(document))
    configuration = _mapping(resolved.get("x-worldforge", {}), "x-worldforge")
    world_id = _text(configuration.get("id", "mcp"), "x-worldforge/id")
    version = _text(configuration.get("version", "1"), "x-worldforge/version")
    title = _text(configuration.get("title", "Static MCP tools"), "x-worldforge/title")
    rules = configuration.get("rules", [])
    if not isinstance(rules, list) or any(not isinstance(rule, str) or not rule for rule in rules):
        raise CompilerError("x-worldforge/rules must be an array of nonempty rule names")
    issues: list[CompatibilityIssue] = []

    def issue(location: str, reason: str, unsupported: bool = False) -> None:
        issues.append(
            CompatibilityIssue(
                location=location,
                severity="unsupported" if unsupported else "warning",
                reason=reason,
            )
        )

    for field in sorted(set(resolved) - {"tools", "$defs", "x-worldforge"}):
        issue(
            f"/{field}", "outside the static MCP tools subset; ignored, never contacted or executed"
        )
    for field in sorted(set(configuration) - {"id", "version", "title", "resources", "rules"}):
        issue(f"/x-worldforge/{field}", "unsupported WorldForge declaration; ignored", True)
    for rule in rules:
        if rule not in REFUND_RULES:
            issue("/x-worldforge/rules", f"rule {rule!r} has no simulator implementation", True)
    raw_resources = _mapping(configuration.get("resources", {}), "x-worldforge/resources")
    if len(raw_resources) > MAX_RESOURCES:
        raise CompilerError(f"MCP resource count exceeds {MAX_RESOURCES}")
    resources: dict[str, WorldResource] = {}
    relationships: list[Relationship] = []
    for name, raw in raw_resources.items():
        location = f"/x-worldforge/resources/{name}"
        if not _RESOURCE_NAME.fullmatch(name):
            raise CompilerError(f"invalid resource name {name!r}")
        schema = _schema(raw, location)
        if schema.get("type") != "object":
            raise CompilerError(f"resource {name!r} must have an object schema")
        resources[name] = WorldResource(
            name=name,
            schema=schema,
            provenance=_origin(
                Origin.DECLARED, source, location, "explicit synthetic resource declaration"
            ),
        )
        properties = _mapping(schema.get("properties", {}), f"{location}/properties")
        for field, value in properties.items():
            if isinstance(value, dict) and "x-worldforge-reference" in value:
                target = _text(value["x-worldforge-reference"], f"{location}/{field}/reference")
                relationships.append(
                    Relationship(
                        resource=name,
                        field=field,
                        target_resource=target,
                        provenance=_origin(
                            Origin.DECLARED,
                            source,
                            f"{location}/properties/{field}",
                            "relationship declared by x-worldforge-reference",
                        ),
                    )
                )
    for relationship in relationships:
        if relationship.target_resource not in resources:
            raise CompilerError(
                f"relationship targets unknown resource {relationship.target_resource!r}"
            )
    tools = resolved.get("tools")
    if not isinstance(tools, list):
        raise CompilerError("tools must be an array")
    if len(tools) > MAX_TOOLS:
        raise CompilerError(f"MCP tool count exceeds {MAX_TOOLS}")
    actions: dict[str, WorldAction] = {}
    descriptions: dict[str, JsonValue] = {}
    for index, raw in enumerate(tools):
        location = f"/tools/{index}"
        tool = _mapping(raw, location)
        name = _text(tool.get("name"), f"{location}/name")
        if len(name.encode("utf-8")) > 128 or any(
            ord(char) < 32 or ord(char) == 127 for char in name
        ):
            raise CompilerError("MCP tool names must be printable and at most 128 UTF-8 bytes")
        if name in actions:
            raise CompilerError(f"duplicate MCP tool name {name!r}")
        for field in ("title", "description"):
            if field in tool and not isinstance(tool[field], str):
                raise CompilerError(f"{location}/{field} must be a string")
        for field in ("annotations", "_meta"):
            if field in tool:
                _mapping(tool[field], f"{location}/{field}")
        unknown_fields = set(tool) - _TOOL_FIELDS
        for field in sorted(unknown_fields):
            issue(f"{location}/{field}", "unsupported MCP tool field; ignored", True)
        input_schema = _schema(tool.get("inputSchema"), f"{location}/inputSchema")
        output_schema = (
            _schema(tool["outputSchema"], f"{location}/outputSchema")
            if "outputSchema" in tool
            else None
        )
        annotation = tool.get("x-worldforge")
        reasons: list[str] = []
        if unknown_fields:
            reasons.append("unsupported MCP tool fields: " + ", ".join(sorted(unknown_fields)))
        kind = "unsupported"
        resource: str | None = None
        if annotation is None:
            reasons.append(
                "tool schema does not declare synthetic business semantics via x-worldforge"
            )
        else:
            annotation = _mapping(annotation, f"{location}/x-worldforge")
            if set(annotation) - {"kind", "resource"}:
                reasons.append("unknown x-worldforge action declaration keys")
            declared_kind = annotation.get("kind")
            if not isinstance(declared_kind, str) or declared_kind not in {
                "create",
                "get",
                "list",
                "refund",
            }:
                reasons.append(f"unknown declared action kind {declared_kind!r}")
            else:
                kind = declared_kind
            raw_resource = annotation.get("resource")
            if not isinstance(raw_resource, str) or raw_resource not in resources:
                reasons.append(f"unknown declared action resource {raw_resource!r}")
            else:
                resource = raw_resource
        reasons.extend(_action_reasons(kind, resource, input_schema, resources, rules))
        reason = "; ".join(reasons) or None
        if reason:
            issue(location, reason, True)
        descriptions[name] = cast(
            JsonValue,
            {
                field: tool[field]
                for field in ("title", "description", "annotations", "_meta")
                if field in tool
            },
        )
        if output_schema is not None:
            cast(dict[str, JsonValue], descriptions[name])["outputSchema"] = output_schema
        actions[name] = WorldAction(
            operation_id=name,
            method="MCP",
            path=f"tool:{name}",
            kind=cast(Any, kind),
            resource=resource,
            input_schema=input_schema,
            response_schemas={"default": output_schema} if output_schema is not None else {},
            provenance=_origin(
                Origin.DECLARED,
                source,
                location,
                "synthetic semantics explicitly declared; interface from MCP tool schema"
                if annotation is not None
                else "interface declared; no synthetic semantics inferred",
            ),
            supported=not reasons,
            reason=reason,
        )
    try:
        world = WorldDefinition(
            world_id=world_id,
            version=version,
            title=title,
            resources=resources,
            actions=actions,
            relationships=relationships,
            rules=rules,
            issues=issues,
            metadata={
                "source": source,
                "compiler_subset": "mcp-static-0.2",
                "mcp_tools": descriptions,
            },
        )
    except ValidationError as exc:
        raise CompilerError(f"invalid compiled MCP world: {exc}") from exc
    _check_compiled_size(world)
    return world
