"""Compile a deliberately bounded, offline OpenAPI subset into WorldForge contracts."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue, ValidationError
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent
from yaml.nodes import MappingNode

from worldforge.models import (
    CompatibilityIssue,
    Origin,
    Provenance,
    Relationship,
    WorldAction,
    WorldDefinition,
    WorldResource,
)

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_NODES = 50_000
MAX_DEPTH = 64
MAX_RESOLVED_NODES = 200_000
MAX_RESOLVED_BYTES = 4 * 1024 * 1024
MAX_WORLD_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 4096
REFUND_RULES = frozenset(
    {
        "refund_payment_exists",
        "refund_amount_positive",
        "refund_total_within_payment",
        "refund_idempotency_key",
    }
)
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class CompilerError(ValueError):
    """An input cannot safely or unambiguously be represented by the compiler."""


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise CompilerError("YAML mapping keys must be strings")
            if key in result:
                raise CompilerError(f"duplicate YAML mapping key: {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CompilerError(f"duplicate JSON mapping key: {key!r}")
        result[key] = value
    return result


def _check_tree(value: Any) -> None:
    """Bound direct Python input too, rejecting cycles and non-JSON values."""
    count = 0
    string_bytes = 0
    active: set[int] = set()

    def check_string(value: str) -> None:
        nonlocal string_bytes
        if len(value) > MAX_DOCUMENT_BYTES:
            raise CompilerError("document exceeds the string byte limit")
        try:
            string_bytes += len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise CompilerError("document strings must be valid UTF-8") from exc
        if string_bytes > MAX_DOCUMENT_BYTES:
            raise CompilerError("document exceeds the string byte limit")

    def visit(node: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise CompilerError("document exceeds the node or nesting limit")
        if isinstance(node, (dict, list)):
            if id(node) in active:
                raise CompilerError("cyclic input containers are not supported")
            active.add(id(node))
            if isinstance(node, dict):
                for key, item in node.items():
                    if not isinstance(key, str):
                        raise CompilerError("document mapping keys must be strings")
                    check_string(key)
                    visit(item, depth + 1)
            else:
                for item in node:
                    visit(item, depth + 1)
            active.remove(id(node))
        elif isinstance(node, str):
            check_string(node)
        elif isinstance(node, float):
            if not math.isfinite(node):
                raise CompilerError("non-finite numbers are not supported")
        elif node is not None and not isinstance(node, (str, int, bool)):
            raise CompilerError(f"non-JSON value: {type(node).__name__}")

    visit(value, 0)


def _load(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            information = os.fstat(handle.fileno())
            if not stat.S_ISREG(information.st_mode):
                raise CompilerError(f"OpenAPI input must be a regular file: {path}")
            if information.st_size > MAX_DOCUMENT_BYTES:
                raise CompilerError(f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise CompilerError(f"cannot read {path}: {exc.strerror}") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise CompilerError(f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
        if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
            document = json.loads(text, object_pairs_hook=_unique_json)
        else:
            depth = 0
            count = 0
            for event in yaml.parse(text, Loader=yaml.SafeLoader):
                count += 1
                if isinstance(event, AliasEvent) or getattr(event, "anchor", None):
                    raise CompilerError("YAML anchors and aliases are not supported")
                if getattr(event, "tag", None):
                    raise CompilerError("explicit YAML tags are not supported")
                if isinstance(event, CollectionStartEvent):
                    depth += 1
                elif isinstance(event, CollectionEndEvent):
                    depth -= 1
                if count > MAX_NODES * 3 or depth > MAX_DEPTH:
                    raise CompilerError("document exceeds the node or nesting limit")
            document = yaml.load(text, Loader=_UniqueSafeLoader)
    except CompilerError:
        raise
    except (UnicodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
        raise CompilerError(f"invalid UTF-8 JSON/YAML document: {exc}") from exc
    _check_tree(document)
    return _mapping(document, "document")


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompilerError(f"{location} must be an object")
    return value


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise CompilerError(f"{location} must be a nonempty string")
    return value


class _Resolver:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self.nodes = 0
        self.string_bytes = 0

    def _account_string(self, value: str) -> None:
        self.string_bytes += len(value.encode("utf-8"))
        if self.string_bytes > MAX_RESOLVED_BYTES:
            raise CompilerError(
                f"reference expansion exceeds the {MAX_RESOLVED_BYTES} byte string/key budget"
            )

    def _pointer(self, reference: str) -> Any:
        if not reference.startswith("#/"):
            raise CompilerError(f"only local JSON Pointer references are supported: {reference}")
        value: Any = self.root
        for part in reference[2:].split("/"):
            if re.search(r"~(?![01])", part):
                raise CompilerError(f"invalid JSON Pointer escape: {reference}")
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict) and key in value:
                value = value[key]
            elif (
                isinstance(value, list)
                and key.isascii()
                and key.isdigit()
                and (key == "0" or not key.startswith("0"))
                and len(key) <= len(str(len(value)))
                and int(key) < len(value)
            ):
                value = value[int(key)]
            else:
                raise CompilerError(f"unresolved reference: {reference}")
        return value

    def resolve(self, value: Any, stack: tuple[str, ...] = (), depth: int = 0) -> Any:
        self.nodes += 1
        if self.nodes > MAX_RESOLVED_NODES or depth > MAX_DEPTH:
            raise CompilerError("reference expansion exceeds the node or nesting limit")
        if isinstance(value, dict):
            unsafe_keywords = {
                "$dynamicRef",
                "$recursiveRef",
                "$id",
                "$anchor",
                "$dynamicAnchor",
                "$recursiveAnchor",
                "$vocabulary",
            }.intersection(value)
            if unsafe_keywords:
                raise CompilerError(
                    "unsupported schema reference/dialect keywords: "
                    + ", ".join(sorted(unsafe_keywords))
                )
            if "$schema" in value and value["$schema"] != (
                "https://json-schema.org/draft/2020-12/schema"
            ):
                raise CompilerError("only the known Draft 2020-12 $schema dialect is supported")
            if "$ref" in value:
                reference = _text(value["$ref"], "$ref")
                if len(value) != 1:
                    raise CompilerError(
                        f"$ref siblings are outside the supported subset: {reference}"
                    )
                if reference in stack:
                    raise CompilerError(f"cyclic reference: {' -> '.join((*stack, reference))}")
                target = self._pointer(reference)
                if not isinstance(target, dict):
                    raise CompilerError(f"reference must resolve to an object: {reference}")
                return self.resolve(target, (*stack, reference), depth + 1)
            result: dict[str, Any] = {}
            for key, item in value.items():
                self._account_string(key)
                result[key] = self.resolve(item, stack, depth + 1)
            return result
        if isinstance(value, list):
            return [self.resolve(item, stack, depth + 1) for item in value]
        if isinstance(value, str):
            self._account_string(value)
        return value


def _check_compiled_size(world: WorldDefinition) -> None:
    """Bound pretty JSON artifacts without constructing one large output string."""
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, indent=2)
    size = 1  # CLI appends a trailing newline.
    try:
        for chunk in encoder.iterencode(world.model_dump(mode="json", by_alias=True)):
            size += len(chunk.encode("utf-8"))
            if size > MAX_WORLD_BYTES:
                raise CompilerError(
                    f"compiled world exceeds the {MAX_WORLD_BYTES} byte serialized output limit"
                )
    except CompilerError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise CompilerError(f"compiled world cannot be safely serialized: {exc}") from exc


def _schema(value: Any, location: str) -> dict[str, JsonValue]:
    schema = _mapping(value, location)

    def normalize(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        result = dict(node)
        if {"pattern", "patternProperties"}.intersection(result):
            raise CompilerError(
                f"{location}: regex schema constraints pattern/patternProperties "
                "are outside the v0.1 bounded validation subset"
            )
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
            if isinstance(result.get(key), dict):
                result[key] = {name: normalize(item) for name, item in result[key].items()}
        for key in (
            "items",
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
            if isinstance(result.get(key), dict):
                result[key] = normalize(result[key])
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if isinstance(result.get(key), list):
                result[key] = [normalize(item) for item in result[key]]
        nullable = result.pop("nullable", False)
        if not isinstance(nullable, bool):
            raise CompilerError(f"{location}: nullable must be boolean")
        if nullable:
            type_value = result.get("type")
            if isinstance(type_value, str):
                result["type"] = [type_value, "null"]
            elif isinstance(type_value, list):
                result["type"] = list(dict.fromkeys([*type_value, "null"]))
            else:
                raise CompilerError(f"{location}: nullable requires an explicit type")
            # OpenAPI nullable does not change an enum's allowed values.
        for bound in ("Minimum", "Maximum"):
            exclusive = f"exclusive{bound}"
            inclusive = bound.lower()
            if isinstance(result.get(exclusive), bool):
                if result.pop(exclusive):
                    if inclusive not in result:
                        raise CompilerError(f"{location}: {exclusive} needs {inclusive}")
                    result[exclusive] = result.pop(inclusive)
        return result

    normalized = cast(dict[str, JsonValue], normalize(schema))
    try:
        Draft202012Validator.check_schema(normalized)
    except SchemaError as exc:
        raise CompilerError(f"invalid schema at {location}: {exc.message}") from exc
    return normalized


def _singular(name: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().replace("-", "_")
    if snake.endswith("ies"):
        return snake[:-3] + "y"
    if snake.endswith("s") and not snake.endswith("ss"):
        return snake[:-1]
    return snake


def _origin(origin: Origin, source: str, location: str, reason: str) -> Provenance:
    return Provenance(origin=origin, source=f"{source}#{location}", reason=reason)


def _input_schema(
    path_item: dict[str, Any], operation: dict[str, Any], location: str
) -> tuple[dict[str, JsonValue], list[str]]:
    properties: dict[str, JsonValue] = {}
    required: list[str] = []
    reasons: list[str] = []
    parameters: dict[tuple[str, str], dict[str, Any]] = {}
    for container in (path_item, operation):
        raw = container.get("parameters", [])
        if not isinstance(raw, list):
            raise CompilerError(f"{location}/parameters must be an array")
        seen: set[tuple[str, str]] = set()
        for index, value in enumerate(raw):
            parameter = _mapping(value, f"{location}/parameters/{index}")
            name = _text(parameter.get("name"), f"{location}/parameter/name")
            where = _text(parameter.get("in"), f"{location}/parameter/in")
            identity = (name, where)
            if identity in seen:
                raise CompilerError(f"duplicate parameter {name!r} in {where} at {location}")
            seen.add(identity)
            parameters[identity] = parameter
    for (name, where), parameter in parameters.items():
        if where not in {"path", "query"}:
            reasons.append(f"parameter {name!r} uses unsupported location {where!r}")
            continue
        if "content" in parameter or "schema" not in parameter:
            reasons.append(f"parameter {name!r} must use a schema")
            continue
        if name in properties:
            reasons.append(f"flattened parameter name collision: {name}")
            continue
        properties[name] = _schema(parameter["schema"], f"{location}/parameters/{name}")
        if parameter.get("required", False) is True or where == "path":
            required.append(name)
    body = operation.get("requestBody")
    if body is not None:
        body = _mapping(body, f"{location}/requestBody")
        content = _mapping(body.get("content", {}), f"{location}/requestBody/content")
        media = content.get("application/json")
        if media is None:
            reasons.append("request body requires application/json")
        else:
            media = _mapping(media, f"{location}/requestBody/content/application~1json")
            body_schema = _schema(media.get("schema", {}), f"{location}/requestBody/schema")
            if body_schema.get("type") != "object":
                reasons.append("request body must be an object for flattened arguments")
            else:
                body_properties = _mapping(
                    body_schema.get("properties", {}), f"{location}/requestBody/properties"
                )
                for name, property_schema in body_properties.items():
                    if name in properties:
                        reasons.append(f"body/parameter name collision: {name}")
                    else:
                        properties[name] = cast(JsonValue, property_schema)
                raw_required = body_schema.get("required", [])
                if isinstance(raw_required, list):
                    required.extend(cast(list[str], raw_required))
                unsupported_keywords = set(body_schema) - {
                    "type",
                    "properties",
                    "required",
                    "additionalProperties",
                    "description",
                    "title",
                    "example",
                    "examples",
                    "deprecated",
                    "readOnly",
                    "writeOnly",
                }
                unsupported_keywords = {
                    key for key in unsupported_keywords if not key.startswith("x-")
                }
                if unsupported_keywords:
                    reasons.append(
                        "body-level constraints cannot be flattened: "
                        + ", ".join(sorted(unsupported_keywords))
                    )
                if body_schema.get("additionalProperties", False) is not False:
                    reasons.append("open-ended request body properties cannot be flattened")
    return {
        "type": "object",
        "properties": properties,
        "required": list(dict.fromkeys(required)),
        "additionalProperties": False,
    }, reasons


def _responses(operation: dict[str, Any], location: str) -> dict[str, dict[str, JsonValue]]:
    responses = _mapping(operation.get("responses", {}), f"{location}/responses")
    result: dict[str, dict[str, JsonValue]] = {}
    for status, raw in responses.items():
        response = _mapping(raw, f"{location}/responses/{status}")
        content = _mapping(response.get("content", {}), f"{location}/responses/{status}/content")
        if "application/json" in content:
            media = _mapping(content["application/json"], f"{location}/responses/{status}/json")
            if "schema" in media:
                result[status] = _schema(media["schema"], f"{location}/responses/{status}/schema")
    return result


def _inferred_kind(
    method: str, path: str, resources: dict[str, WorldResource]
) -> tuple[str, str | None]:
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return "unsupported", None
    name = _singular(segments[0])
    if name not in resources:
        return "unsupported", None
    if len(segments) == 1:
        if method == "post":
            return "create", name
        if method == "get":
            return "list", name
    if len(segments) == 2 and segments[1] == "{id}" and method == "get":
        return "get", name
    return "unsupported", name


def compile_openapi(path: Path | str) -> WorldDefinition:
    """Read a bounded UTF-8 document without making network requests."""
    source = Path(path)
    return compile_document(_load(source), source=str(source))


def compile_document(document: dict[str, Any], source: str = "memory") -> WorldDefinition:
    """Resolve local acyclic references and compile declared or visible CRUD semantics."""
    _check_tree(document)
    _mapping(document, "document")
    _text(source, "source")
    try:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise CompilerError(f"source name exceeds the {MAX_SOURCE_BYTES} byte limit")
    except UnicodeError as exc:
        raise CompilerError("source name must be valid UTF-8") from exc
    version = _text(document.get("openapi"), "openapi")
    if not re.fullmatch(r"3\.(?:0|1)\.\d+", version):
        raise CompilerError(f"supported OpenAPI versions are 3.0.x and 3.1.x, got {version!r}")
    if document.get("jsonSchemaDialect", "https://json-schema.org/draft/2020-12/schema") != (
        "https://json-schema.org/draft/2020-12/schema"
    ):
        raise CompilerError("only the known Draft 2020-12 schema dialect is supported")
    resolved = cast(dict[str, Any], _Resolver(document).resolve(document))
    info = _mapping(resolved.get("info", {}), "info")
    title = _text(info.get("title"), "info/title")
    configuration = _mapping(resolved.get("x-worldforge", {}), "x-worldforge")
    world_id = _text(configuration.get("id", _singular(title.replace(" ", "_"))), "world id")
    world_version = _text(configuration.get("version", info.get("version", "1")), "world version")
    rules = configuration.get("rules", [])
    if not isinstance(rules, list) or any(not isinstance(rule, str) or not rule for rule in rules):
        raise CompilerError("x-worldforge/rules must be an array of nonempty rule names")
    issues = [
        CompatibilityIssue(
            location="/x-worldforge/rules",
            severity="unsupported",
            reason=f"rule {rule!r} has no v0.1 implementation",
        )
        for rule in rules
        if rule not in REFUND_RULES
    ]
    components = _mapping(resolved.get("components", {}), "components")
    schemas = _mapping(components.get("schemas", {}), "components/schemas")
    resources: dict[str, WorldResource] = {}
    relationships: list[Relationship] = []
    for component, raw_schema in schemas.items():
        location = f"/components/schemas/{component}"
        schema = _schema(raw_schema, location)
        declaration = schema.get("x-worldforge-resource")
        if declaration is False:
            continue
        if declaration is not None:
            name = _text(declaration, f"{location}/x-worldforge-resource")
            origin = Origin.DECLARED
            reason = "resource name declared by x-worldforge-resource"
        else:
            if schema.get("type") != "object":
                continue
            name = _singular(component)
            origin = Origin.INFERRED
            reason = "resource name inferred from object component name"
        if not _NAME.fullmatch(name):
            raise CompilerError(f"invalid resource name {name!r} at {location}")
        if schema.get("type") != "object":
            raise CompilerError(f"resource {name!r} must have an object schema")
        if name in resources:
            raise CompilerError(f"duplicate resource name {name!r}")
        resources[name] = WorldResource(
            name=name,
            schema=schema,
            provenance=_origin(origin, source, location, reason),
        )
        properties = _mapping(schema.get("properties", {}), f"{location}/properties")
        for field, property_schema in properties.items():
            if isinstance(property_schema, bool):
                continue
            property_schema = _mapping(property_schema, f"{location}/properties/{field}")
            target = property_schema.get("x-worldforge-reference")
            if target is not None:
                relationships.append(
                    Relationship(
                        resource=name,
                        field=field,
                        target_resource=_text(target, f"{location}/properties/{field}/reference"),
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
                f"relationship {relationship.resource}.{relationship.field} targets unknown "
                f"resource {relationship.target_resource!r}"
            )
    actions: dict[str, WorldAction] = {}
    paths = _mapping(resolved.get("paths", {}), "paths")
    for path, raw_path in paths.items():
        if not path.startswith("/"):
            raise CompilerError(f"path must start with '/': {path!r}")
        path_item = _mapping(raw_path, f"/paths/{path}")
        for method, raw_operation in path_item.items():
            if method not in _METHODS:
                continue
            location = f"/paths/{path}/{method}"
            operation = _mapping(raw_operation, location)
            operation_id = operation.get("operationId")
            if operation_id is None:
                operation_id = method + "_" + re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
            operation_id = _text(operation_id, f"{location}/operationId")
            if operation_id in actions:
                raise CompilerError(f"duplicate operationId {operation_id!r}")
            reasons: list[str] = []
            annotation = operation.get("x-worldforge")
            if annotation is not None:
                annotation = _mapping(annotation, f"{location}/x-worldforge")
                kind = annotation.get("kind")
                resource = annotation.get("resource")
                origin = Origin.DECLARED
                provenance_reason = "executable semantics declared by x-worldforge"
                if not isinstance(kind, str) or kind not in {"create", "get", "list", "refund"}:
                    reasons.append(f"unknown declared action kind {kind!r}")
                    kind = "unsupported"
                if not isinstance(resource, str) or resource not in resources:
                    reasons.append(f"unknown declared action resource {resource!r}")
                    resource = None
            else:
                kind, resource = _inferred_kind(method, path, resources)
                origin = Origin.INFERRED
                provenance_reason = "semantics inferred from a simple collection/item CRUD route"
                if kind == "unsupported":
                    reasons.append("operation has no declared or unambiguous CRUD semantics")
                    provenance_reason = "no supported semantics could be inferred"
            input_schema, input_reasons = _input_schema(path_item, operation, location)
            reasons.extend(input_reasons)
            properties = cast(dict[str, JsonValue], input_schema["properties"])
            required = cast(list[str], input_schema["required"])
            if kind in {"create", "refund"} and method != "post":
                reasons.append(f"{kind} requires POST")
            if kind in {"get", "list"} and method != "get":
                reasons.append(f"{kind} requires GET")
            if kind == "get" and ("id" not in properties or "id" not in required):
                reasons.append("get requires a required argument named id")
            if kind == "get":
                if set(properties) != {"id"}:
                    reasons.append("get only implements the id argument")
                identifier = properties.get("id")
                if not isinstance(identifier, dict) or identifier.get("type") != "string":
                    reasons.append("get requires an id argument with string type")
            if kind == "list":
                allowed_filters = {"payment": {"customer_id"}, "refund": {"payment_id"}}
                unsupported_filters = set(properties) - allowed_filters.get(resource or "", set())
                if unsupported_filters:
                    reasons.append(
                        "list does not implement filters: " + ", ".join(sorted(unsupported_filters))
                    )
                for name, filter_schema in properties.items():
                    if not isinstance(filter_schema, dict) or filter_schema.get("type") != "string":
                        reasons.append(f"list filter {name} requires string type")
            if kind == "refund":
                missing = REFUND_RULES - set(rules)
                if missing:
                    reasons.append(
                        "refund requires explicitly declared rules: " + ", ".join(sorted(missing))
                    )
                if resource != "refund":
                    reasons.append("refund semantics require the refund resource")
                if "payment" not in resources:
                    reasons.append("refund semantics require the payment resource")
                if not {"payment_id", "amount", "idempotency_key"}.issubset(required):
                    reasons.append(
                        "refund requires payment_id, amount, and idempotency_key arguments"
                    )
                if set(properties) != {"payment_id", "amount", "idempotency_key"}:
                    reasons.append("refund only implements payment_id, amount, and idempotency_key")
                for field, field_type in (
                    ("payment_id", "string"),
                    ("amount", "integer"),
                    ("idempotency_key", "string"),
                ):
                    field_schema = properties.get(field)
                    if not isinstance(field_schema, dict) or field_schema.get("type") != field_type:
                        reasons.append(f"refund {field} requires {field_type} type")
            unsupported = bool(reasons)
            action_reason = "; ".join(reasons) if unsupported else None
            if action_reason:
                issues.append(
                    CompatibilityIssue(
                        location=location,
                        severity="unsupported",
                        reason=action_reason,
                    )
                )
            actions[operation_id] = WorldAction(
                operation_id=operation_id,
                method=method.upper(),
                path=path,
                kind=cast(Any, kind),
                resource=resource,
                input_schema=input_schema,
                response_schemas=_responses(operation, location),
                provenance=_origin(origin, source, location, provenance_reason),
                supported=not unsupported,
                reason=action_reason,
            )
    try:
        world = WorldDefinition(
            world_id=world_id,
            version=world_version,
            title=title,
            resources=resources,
            actions=actions,
            relationships=relationships,
            rules=rules,
            issues=issues,
            metadata={"openapi_version": version, "source": source, "compiler_subset": "0.1"},
        )
    except ValidationError as exc:
        raise CompilerError(f"invalid compiled world: {exc}") from exc
    _check_compiled_size(world)
    return world
