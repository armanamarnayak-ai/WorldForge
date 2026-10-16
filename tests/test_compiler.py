from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from worldforge.compiler import MAX_DOCUMENT_BYTES, CompilerError, compile_document, compile_openapi
from worldforge.models import Origin

SAMPLE = Path(__file__).resolve().parents[1] / "examples/payments/openapi.yaml"


def sample() -> dict[str, Any]:
    return yaml.safe_load(SAMPLE.read_text())  # type: ignore[no-any-return]


def minimal() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Test", "version": "1"},
        "components": {
            "schemas": {
                "Widgets": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                }
            }
        },
        "paths": {
            "/widgets": {
                "get": {"operationId": "list_widgets", "responses": {}},
            },
        },
    }


def test_sample_compiles_complete_resolved_typed_world() -> None:
    world = compile_openapi(SAMPLE)
    assert world.world_id == "payments"
    assert world.version == "1"
    assert set(world.resources) == {"customer", "payment", "refund"}
    assert len(world.actions) == 8
    assert not world.issues
    assert all(action.supported for action in world.actions.values())
    assert all(action.provenance.origin == Origin.DECLARED for action in world.actions.values())
    assert {(r.resource, r.field, r.target_resource) for r in world.relationships} == {
        ("payment", "customer_id", "customer"),
        ("payment", "duplicate_of", "payment"),
        ("refund", "payment_id", "payment"),
    }
    assert "$ref" not in world.model_dump_json()
    assert world.actions["get_payment"].input_schema["required"] == ["id"]
    assert set(world.actions["create_refund"].response_schemas) == {"201"}
    assert (
        world.actions["create_refund"].response_schemas["201"] == world.resources["refund"].schema_
    )
    payment = world.resources["payment"].schema_
    assert payment["properties"]["duplicate_of"]["type"] == ["string", "null"]  # type: ignore[index]
    Draft202012Validator(payment).validate(
        {
            "id": "pay_1",
            "customer_id": "cus_1",
            "amount": 1500,
            "currency": "USD",
            "status": "succeeded",
            "duplicate_of": None,
        }
    )


def test_inference_is_visible_and_unknown_business_action_is_unsupported() -> None:
    document = minimal()
    document["paths"]["/widgets/reconcile"] = {
        "post": {"operationId": "reconcile", "responses": {}}
    }
    world = compile_document(document)
    assert world.resources["widget"].provenance.origin == Origin.INFERRED
    assert world.actions["list_widgets"].kind == "list"
    assert world.actions["list_widgets"].provenance.origin == Origin.INFERRED
    assert not world.actions["reconcile"].supported
    assert world.actions["reconcile"].kind == "unsupported"
    assert world.issues[0].severity == "unsupported"


@pytest.mark.parametrize(
    "reference", ["https://example.org/schema", "other.yaml#/Thing", "#/missing"]
)
def test_reference_errors_are_useful(reference: str) -> None:
    document = minimal()
    document["components"]["schemas"]["Widgets"] = {"$ref": reference}
    with pytest.raises(CompilerError, match="reference"):
        compile_document(document)


def test_cycles_are_rejected_in_unused_definitions() -> None:
    document = minimal()
    document["components"]["schemas"]["Cycle"] = {
        "type": "object",
        "properties": {"next": {"$ref": "#/components/schemas/Cycle"}},
    }
    with pytest.raises(CompilerError, match="cyclic reference"):
        compile_document(document)


def test_reference_expansion_is_bounded() -> None:
    document = minimal()
    schemas = document["components"]["schemas"]
    schemas["Level0"] = {"type": "string"}
    for index in range(1, 20):
        schemas[f"Level{index}"] = {
            "type": "object",
            "x-worldforge-resource": False,
            "properties": {
                str(n): {"$ref": f"#/components/schemas/Level{index - 1}"} for n in range(4)
            },
        }
    with pytest.raises(CompilerError, match="expansion exceeds"):
        compile_document(document)


def test_refund_semantics_are_not_invented() -> None:
    document = sample()
    document["x-worldforge"]["rules"] = []
    world = compile_document(document)
    assert not world.actions["create_refund"].supported
    assert "explicitly declared rules" in (world.actions["create_refund"].reason or "")
    assert world.actions["list_payments"].supported


def test_body_parameter_collision_is_inspectable_and_not_executable() -> None:
    document = sample()
    document["paths"]["/refunds"]["post"]["parameters"] = [
        {"name": "payment_id", "in": "query", "schema": {"type": "string"}},
    ]
    world = compile_document(document)
    action = world.actions["create_refund"]
    assert not action.supported
    assert "name collision: payment_id" in (action.reason or "")


def test_parameter_override_and_resolved_response_schema() -> None:
    document = sample()
    operation = document["paths"]["/payments/{id}"]["get"]
    operation["parameters"] = [
        {
            "name": "id",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "minLength": 4},
        },
    ]
    world = compile_document(document)
    arguments = world.actions["get_payment"].input_schema
    assert arguments["properties"]["id"]["minLength"] == 4  # type: ignore[index]
    assert world.actions["get_payment"].supported


def test_unknown_relationship_target_fails() -> None:
    document = sample()
    document["components"]["schemas"]["Payment"]["properties"]["customer_id"][
        "x-worldforge-reference"
    ] = "missing"
    with pytest.raises(CompilerError, match="targets unknown resource"):
        compile_document(document)


def test_compile_does_not_mutate_input() -> None:
    document = sample()
    before = copy.deepcopy(document)
    compile_document(document)
    assert document == before


def test_json_and_yaml_compile_identically(tmp_path: Path) -> None:
    document = sample()
    target = tmp_path / "openapi.json"
    target.write_text(json.dumps(document))
    json_world = compile_openapi(target)
    yaml_world = compile_openapi(SAMPLE)
    assert set(json_world.actions) == set(yaml_world.actions)
    assert json_world.resources["payment"].schema_ == yaml_world.resources["payment"].schema_


@pytest.mark.parametrize(
    "text",
    [
        "openapi: 3.0.3\ninfo: &data {title: Test, version: '1'}\npaths: *data\n",
        "openapi: !!str 3.0.3\n",
        "openapi: 3.0.3\nopenapi: 3.1.0\n",
        '{"openapi":"3.0.3","openapi":"3.1.0"}',
    ],
)
def test_unsafe_or_ambiguous_documents_fail(tmp_path: Path, text: str) -> None:
    target = tmp_path / "openapi.yaml"
    target.write_text(text)
    with pytest.raises(CompilerError):
        compile_openapi(target)


def test_document_byte_limit(tmp_path: Path) -> None:
    target = tmp_path / "openapi.yaml"
    target.write_bytes(b" " * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(CompilerError, match="bytes"):
        compile_openapi(target)


def test_python_container_cycles_and_deep_trees_fail() -> None:
    cyclic = minimal()
    cyclic["cycle"] = cyclic
    with pytest.raises(CompilerError, match="cyclic input"):
        compile_document(cyclic)
    deep = minimal()
    container: dict[str, Any] = deep
    for _ in range(70):
        nested: dict[str, Any] = {}
        container["nested"] = nested
        container = nested
    with pytest.raises(CompilerError, match="nesting limit"):
        compile_document(deep)


def test_duplicate_operation_ids_fail() -> None:
    document = sample()
    document["paths"]["/payments"]["post"]["operationId"] = "list_payments"
    with pytest.raises(CompilerError, match="duplicate operationId"):
        compile_document(document)


@pytest.mark.parametrize(
    "keyword",
    [
        "$dynamicRef",
        "$recursiveRef",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$vocabulary",
    ],
)
def test_alternate_reference_mechanisms_cannot_enable_network_access(keyword: str) -> None:
    document = minimal()
    document["components"]["schemas"]["Widgets"][keyword] = "https://example.org/untrusted"
    with pytest.raises(CompilerError, match="unsupported schema reference/dialect"):
        compile_document(document)


@pytest.mark.parametrize("where", ["schema", "document"])
def test_custom_schema_dialects_are_rejected(where: str) -> None:
    document = minimal()
    if where == "schema":
        document["components"]["schemas"]["Widgets"]["$schema"] = "https://example.org/meta"
    else:
        document["jsonSchemaDialect"] = "https://example.org/meta"
    with pytest.raises(CompilerError, match="dialect"):
        compile_document(document)


def test_normalization_preserves_data_and_properties_named_nullable() -> None:
    document = minimal()
    schema = document["components"]["schemas"]["Widgets"]
    schema["properties"]["nullable"] = {"type": "boolean"}
    schema["default"] = {"nullable": True}
    world = compile_document(document)
    assert world.resources["widget"].schema_["properties"] == schema["properties"]
    assert world.resources["widget"].schema_["default"] == {"nullable": True}


def test_unimplemented_list_filter_is_unsupported() -> None:
    document = minimal()
    document["paths"]["/widgets"]["get"]["parameters"] = [
        {"name": "name", "in": "query", "schema": {"type": "string"}},
    ]
    action = compile_document(document).actions["list_widgets"]
    assert not action.supported
    assert "does not implement filters: name" in (action.reason or "")


def test_integer_get_id_is_unsupported() -> None:
    document = sample()
    document["paths"]["/payments/{id}"]["parameters"][0]["schema"] = {"type": "integer"}
    action = compile_document(document).actions["get_payment"]
    assert not action.supported
    assert "string type" in (action.reason or "")


def test_extra_get_argument_is_preserved_but_unsupported() -> None:
    document = sample()
    document["paths"]["/payments/{id}"]["get"]["parameters"] = [
        {"name": "expand", "in": "query", "schema": {"type": "string"}},
    ]
    action = compile_document(document).actions["get_payment"]
    assert not action.supported
    assert "only implements the id argument" in (action.reason or "")
    assert "expand" in action.input_schema["properties"]  # type: ignore[operator]


def test_extra_refund_argument_is_unsupported() -> None:
    document = sample()
    body = document["paths"]["/refunds"]["post"]["requestBody"]["content"]["application/json"]
    body["schema"]["properties"]["reason"] = {"type": "string"}
    action = compile_document(document).actions["create_refund"]
    assert not action.supported
    assert "only implements payment_id" in (action.reason or "")


@pytest.mark.parametrize("keyword", ["pattern", "patternProperties"])
def test_regex_constraints_are_rejected_instead_of_unbounded_validation(keyword: str) -> None:
    document = minimal()
    schema = document["components"]["schemas"]["Widgets"]
    if keyword == "pattern":
        schema["properties"]["id"][keyword] = "(a+)+$"
    else:
        schema[keyword] = {"(a+)+$": {"type": "string"}}
    with pytest.raises(CompilerError, match="bounded validation subset"):
        compile_document(document)


def test_regex_named_literal_data_is_preserved() -> None:
    document = minimal()
    schema = document["components"]["schemas"]["Widgets"]
    schema["properties"]["pattern"] = {"type": "string"}
    schema["example"] = {"pattern": "(a+)+$"}
    world = compile_document(document)
    assert world.resources["widget"].schema_["example"] == {"pattern": "(a+)+$"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX-only special-file regression")
def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    target = tmp_path / "openapi.yaml"
    os.mkfifo(target)
    with pytest.raises(CompilerError, match="regular file"):
        compile_openapi(target)


@pytest.mark.parametrize("index", ["00", "9" * 5000, "١"])
def test_json_pointer_array_indices_are_canonical_and_bounded(index: str) -> None:
    document = minimal()
    document["definitions"] = [{"type": "object"}]
    document["components"]["schemas"]["Widgets"] = {"$ref": "#/definitions/" + index}
    with pytest.raises(CompilerError, match="unresolved reference"):
        compile_document(document)


@pytest.mark.parametrize("keyword", ["description", "default", "enum"])
def test_reference_expansion_string_amplification_is_bounded(keyword: str) -> None:
    document = minimal()
    scalar: dict[str, Any] = {"type": "string", "x-worldforge-resource": False}
    scalar[keyword] = ["x" * 16_384] if keyword == "enum" else "x" * 16_384
    document["components"]["schemas"]["Scalar"] = scalar
    document["components"]["schemas"]["Widgets"]["properties"] = {
        f"field_{index}": {"$ref": "#/components/schemas/Scalar"} for index in range(520)
    }
    assert len(json.dumps(document).encode("utf-8")) < 64 * 1024
    with pytest.raises(CompilerError, match="byte string/key budget"):
        compile_document(document)


def test_compiled_artifact_has_independent_serialized_output_budget() -> None:
    document = minimal()
    document["paths"] = {
        f"/widgets/{index}": {
            "get": {
                "operationId": f"list_widgets_{index}",
                "x-worldforge": {"kind": "list", "resource": "widget"},
                "responses": {},
            },
        }
        for index in range(1100)
    }
    with pytest.raises(CompilerError, match="serialized output limit"):
        compile_document(document, source="x" * 4096)


def test_unbounded_provenance_source_is_rejected() -> None:
    with pytest.raises(CompilerError, match="source name exceeds"):
        compile_document(minimal(), source="x" * 4097)


@pytest.mark.parametrize("kind", [[], {}, True, 1, None])
def test_malformed_declared_action_kind_is_transparently_unsupported(kind: Any) -> None:
    document = sample()
    document["paths"]["/customers"]["post"]["x-worldforge"]["kind"] = kind
    world = compile_document(document)
    action = world.actions["create_customer"]
    assert not action.supported
    assert action.kind == "unsupported"
    assert "unknown declared action kind" in (action.reason or "")
    assert any("unknown declared action kind" in issue.reason for issue in world.issues)
