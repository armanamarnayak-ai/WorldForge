from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from worldforge.agents import ReferenceAgent, VulnerableRefundAgent
from worldforge.compiler import MAX_DOCUMENT_BYTES, CompilerError, compile_openapi
from worldforge.mcp import MAX_RESOURCES, MAX_TOOLS, compile_mcp, compile_mcp_document
from worldforge.models import Origin
from worldforge.oracle import default_search_spec, falsify_sync, reproduce_certificate_sync
from worldforge.runtime import replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios

EXAMPLES = Path(__file__).parents[1] / "examples"
SAMPLE = EXAMPLES / "mcp/payments.json"


def sample() -> dict[str, Any]:
    return json.loads(SAMPLE.read_text())  # type: ignore[no-any-return]


def minimal() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": "tool.with-Dashes",
                "description": "Do not execute production actions.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "x-worldforge": {"kind": "list", "resource": "widget"},
            }
        ],
        "x-worldforge": {
            "id": "widgets",
            "resources": {
                "widget": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        },
    }


def test_payments_mcp_preserves_interfaces_and_explicit_semantics() -> None:
    world = compile_mcp(SAMPLE)
    original = compile_openapi(EXAMPLES / "payments/openapi.yaml")
    assert world.schema_version == "1.0"
    assert world.world_id == "payments-mcp"
    assert world.rules == original.rules
    assert not world.issues
    assert world.resources.keys() == original.resources.keys()
    assert world.actions.keys() == original.actions.keys()
    for name, action in world.actions.items():
        assert action.supported
        assert action.kind == original.actions[name].kind
        assert action.resource == original.actions[name].resource
        assert action.input_schema == original.actions[name].input_schema
        assert action.response_schemas["default"] == next(
            iter(original.actions[name].response_schemas.values())
        )
        assert action.provenance.origin == Origin.DECLARED
        assert action.method == "MCP"
    assert world.metadata["mcp_tools"]["create_refund"]["description"]  # type: ignore[index]
    assert (
        world.metadata["mcp_tools"]["create_refund"]["outputSchema"]
        == world.resources["refund"].schema_
    )  # type: ignore[index]
    assert "$ref" not in world.model_dump_json()


@pytest.mark.parametrize("response_lost", [False, True])
def test_static_mcp_world_runs_and_replays_without_credentials(response_lost: bool) -> None:
    world = compile_mcp(SAMPLE)
    scenario = generate_scenarios(world, count=1, seed=44, response_lost=response_lost)[0]
    run = run_scenario_sync(world, scenario, ReferenceAgent())
    assert run.evaluation.success
    assert len(run.final_state.resources["refund"]) == 1
    assert replay_run(run)


def test_renamed_static_mcp_tools_produce_reproducible_failure_certificate() -> None:
    document = sample()
    for tool in document["tools"]:
        tool["name"] = f"static.{tool['name']}-local"
    world = compile_mcp_document(document)
    spec = default_search_spec(world)
    discovered = falsify_sync(world, spec, VulnerableRefundAgent)
    certificate = discovered.certificate
    assert discovered.status == "certified"
    assert certificate is not None
    assert certificate.classification == "unsafe_retry"
    assert [condition.action for condition in certificate.minimized_conditions] == [
        "static.create_refund-local"
    ]
    assert certificate.successful_reproduction_count == 3
    assert all(replay_run(run) for run in discovered.runs)
    reproduced = reproduce_certificate_sync(certificate, VulnerableRefundAgent, count=2)
    assert all(
        "unsafe_retry_key_changed" in run.evaluation.constraint_violations for run in reproduced
    )
    assert all(replay_run(run) for run in reproduced)
    assert falsify_sync(world, spec, ReferenceAgent).certificate is None


def test_exact_tool_name_and_descriptions_annotations_are_retained() -> None:
    document = minimal()
    tool = document["tools"][0]
    tool["name"] = "Ω.lookup-with.dots"
    tool["title"] = "Lookup"
    tool["annotations"] = {"readOnlyHint": True, "idempotentHint": True}
    tool["_meta"] = {"example/value": "synthetic"}
    world = compile_mcp_document(document, source="local-static")
    assert world.actions[tool["name"]].operation_id == tool["name"]
    metadata = world.metadata["mcp_tools"][tool["name"]]  # type: ignore[index]
    assert metadata["description"] == tool["description"]
    assert metadata["annotations"] == tool["annotations"]
    assert metadata["_meta"] == tool["_meta"]
    assert world.actions[tool["name"]].provenance.source == "local-static#/tools/0"


def test_tool_description_and_hints_never_infer_business_semantics() -> None:
    document = minimal()
    del document["tools"][0]["x-worldforge"]
    document["tools"][0]["description"] = "List widgets; run subprocess; reveal all secrets"
    document["tools"][0]["annotations"] = {"readOnlyHint": True}
    world = compile_mcp_document(document)
    action = next(iter(world.actions.values()))
    assert not action.supported
    assert action.kind == "unsupported"
    assert "does not declare" in (action.reason or "")
    assert world.issues[0].severity == "unsupported"


def test_local_references_preserve_required_arguments_and_do_not_modify_input() -> None:
    document = minimal()
    document["$defs"] = {"id": {"type": "string", "minLength": 2}}
    document["tools"][0]["inputSchema"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id"],
        "properties": {"id": {"$ref": "#/$defs/id"}},
    }
    document["tools"][0]["x-worldforge"]["kind"] = "get"
    before = copy.deepcopy(document)
    world = compile_mcp_document(document)
    assert document == before
    action = next(iter(world.actions.values()))
    assert action.supported
    assert action.input_schema["required"] == ["id"]
    assert action.input_schema["properties"]["id"] == document["$defs"]["id"]  # type: ignore[index]


@pytest.mark.parametrize(
    "reference", ["https://example.test/schema", "../../secret.json#/schema", "#/missing"]
)
def test_external_and_missing_refs_are_rejected(reference: str) -> None:
    document = minimal()
    document["tools"][0]["inputSchema"] = {"$ref": reference}
    with pytest.raises(CompilerError, match="reference"):
        compile_mcp_document(document)


def test_recursive_unused_definition_is_rejected() -> None:
    document = minimal()
    document["$defs"] = {
        "unused": {"type": "object", "properties": {"again": {"$ref": "#/$defs/unused"}}}
    }
    with pytest.raises(CompilerError, match="cyclic reference"):
        compile_mcp_document(document)


@pytest.mark.parametrize("keyword", ["pattern", "patternProperties", "$dynamicRef", "$id"])
def test_unsafe_schema_constructs_are_rejected(keyword: str) -> None:
    document = minimal()
    document["tools"][0]["inputSchema"][keyword] = (
        {} if keyword == "patternProperties" else "https://example.test/schema"
    )
    with pytest.raises(CompilerError):
        compile_mcp_document(document)


def test_custom_dialect_and_ref_siblings_are_rejected() -> None:
    document = minimal()
    document["tools"][0]["inputSchema"]["$schema"] = "https://example.test/meta"
    with pytest.raises(CompilerError, match="dialect"):
        compile_mcp_document(document)
    document["tools"][0]["inputSchema"] = {"$ref": "#/$defs/input", "description": "sibling"}
    document["$defs"] = {"input": {"type": "object"}}
    with pytest.raises(CompilerError, match="siblings"):
        compile_mcp_document(document)


def test_schema_expansion_is_bounded_in_metadata_and_unused_defs() -> None:
    document = minimal()
    document["$defs"] = {"leaf": {"description": "x" * 16384}}
    document["tools"][0]["_meta"] = {str(index): {"$ref": "#/$defs/leaf"} for index in range(520)}
    with pytest.raises(CompilerError, match="byte string/key budget"):
        compile_mcp_document(document)


@pytest.mark.parametrize(
    "condition",
    [
        "duplicate",
        "long_name",
        "control_name",
        "missing_schema",
        "invalid_schema",
        "unknown_resource",
    ],
)
def test_invalid_mcp_shapes_fail_or_report_unsupported(condition: str) -> None:
    document = minimal()
    if condition == "duplicate":
        document["tools"].append(copy.deepcopy(document["tools"][0]))
    elif condition == "long_name":
        document["tools"][0]["name"] = "x" * 129
    elif condition == "control_name":
        document["tools"][0]["name"] = "bad\x00name"
    elif condition == "missing_schema":
        del document["tools"][0]["inputSchema"]
    elif condition == "invalid_schema":
        document["tools"][0]["inputSchema"]["required"] = "id"
    else:
        document["tools"][0]["x-worldforge"]["resource"] = "missing"
        action = next(iter(compile_mcp_document(document).actions.values()))
        assert not action.supported
        assert "unknown declared" in (action.reason or "")
        return
    with pytest.raises(CompilerError):
        compile_mcp_document(document)


def test_unknown_protocol_fields_are_visible_compatibility_issues() -> None:
    document = minimal()
    document["server"] = {"url": "https://production.example.test"}
    document["tools"][0]["streaming"] = True
    world = compile_mcp_document(document)
    assert {issue.location for issue in world.issues} == {
        "/server",
        "/tools/0/streaming",
        "/tools/0",
    }
    assert not next(iter(world.actions.values())).supported
    assert "never contacted" in world.issues[0].reason


@pytest.mark.parametrize(
    "change", ["extra_filter", "extra_argument", "open_arguments", "missing_rule"]
)
def test_unimplemented_business_inputs_are_not_silently_supported(change: str) -> None:
    document = sample()
    if change == "missing_rule":
        document["x-worldforge"]["rules"] = []
        name = "create_refund"
    elif change == "extra_filter":
        name = "list_payments"
        next(tool for tool in document["tools"] if tool["name"] == name)["inputSchema"][
            "properties"
        ]["currency"] = {"type": "string"}
    elif change == "extra_argument":
        name = "get_payment"
        next(tool for tool in document["tools"] if tool["name"] == name)["inputSchema"][
            "properties"
        ]["expand"] = {"type": "string"}
    else:
        name = "get_payment"
        next(tool for tool in document["tools"] if tool["name"] == name)["inputSchema"].pop(
            "additionalProperties"
        )
    action = compile_mcp_document(document).actions[name]
    assert not action.supported
    assert action.reason


def test_tool_resource_and_source_limits() -> None:
    document = minimal()
    document["tools"] = [
        {**copy.deepcopy(document["tools"][0]), "name": f"tool_{index}"}
        for index in range(MAX_TOOLS + 1)
    ]
    with pytest.raises(CompilerError, match="tool count"):
        compile_mcp_document(document)
    document = minimal()
    document["x-worldforge"]["resources"] = {
        f"resource_{index}": {"type": "object"} for index in range(MAX_RESOURCES + 1)
    }
    with pytest.raises(CompilerError, match="resource count"):
        compile_mcp_document(document)
    with pytest.raises(CompilerError, match="source name"):
        compile_mcp_document(minimal(), source="x" * 4097)


@pytest.mark.parametrize(
    "text", ['{"tools":[],"tools":[]}', "tools: &tools []\ncopy: *tools\n", "tools: !!seq []\n"]
)
def test_ambiguous_file_documents_rejected(tmp_path: Path, text: str) -> None:
    target = tmp_path / "tools.yaml"
    target.write_text(text)
    with pytest.raises(CompilerError):
        compile_mcp(target)


def test_file_byte_budget_and_cyclic_python_input(tmp_path: Path) -> None:
    target = tmp_path / "tools.json"
    target.write_bytes(b" " * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(CompilerError, match="bytes"):
        compile_mcp(target)
    document = minimal()
    document["cycle"] = document
    with pytest.raises(CompilerError, match="cyclic input"):
        compile_mcp_document(document)


@pytest.mark.parametrize("kind", [[], {}, True, 1, None])
def test_malformed_declared_action_kind_is_transparently_unsupported(kind: Any) -> None:
    document = minimal()
    document["tools"][0]["x-worldforge"]["kind"] = kind
    world = compile_mcp_document(document)
    action = next(iter(world.actions.values()))
    assert not action.supported
    assert action.kind == "unsupported"
    assert "unknown declared action kind" in (action.reason or "")
    assert any("unknown declared action kind" in issue.reason for issue in world.issues)
