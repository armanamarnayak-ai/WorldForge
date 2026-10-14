import pytest
from pydantic import ValidationError

from worldforge.models import Origin, Provenance, WorldAction, WorldDefinition, WorldResource


def example_world():
    provenance = Provenance(origin=Origin.DECLARED, source="test", reason="test contract")
    return WorldDefinition(
        world_id="demo",
        title="Demo",
        resources={
            "customer": WorldResource(
                name="customer", schema={"type": "object"}, provenance=provenance
            )
        },
        actions={
            "create_customer": WorldAction(
                operation_id="create_customer",
                method="POST",
                path="/customers",
                kind="create",
                resource="customer",
                provenance=provenance,
            )
        },
    )


def test_world_roundtrip_preserves_schema_and_provenance():
    world = example_world()
    assert WorldDefinition.model_validate_json(world.model_dump_json(by_alias=True)) == world
    assert world.model_dump(by_alias=True)["resources"]["customer"]["schema"] == {"type": "object"}
    assert WorldDefinition.model_validate_json(world.model_dump_json()) == world


def test_unknown_version_and_fields_fail():
    data = example_world().model_dump(by_alias=True)
    data["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        WorldDefinition.model_validate(data)
    data["schema_version"] = "1.0"
    data["secret"] = "unexpected"
    with pytest.raises(ValidationError):
        WorldDefinition.model_validate(data)


def test_unknown_action_resource_fails():
    data = example_world().model_dump(by_alias=True)
    data["actions"]["create_customer"]["resource"] = "missing"
    with pytest.raises(ValidationError, match="unknown resource"):
        WorldDefinition.model_validate(data)
