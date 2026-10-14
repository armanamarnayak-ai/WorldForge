import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from worldforge.models import ResourceUsage, Run, Scenario, WorldDefinition


@pytest.mark.parametrize(
    "name,model", [("world", WorldDefinition), ("scenario", Scenario), ("run", Run)]
)
def test_checked_in_schemas_match_public_contract(name, model):
    path = Path(__file__).resolve().parents[1] / "schemas" / f"{name}-1.0.json"
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    schema.pop("$id")
    schema.pop("$schema")
    assert schema == model.model_json_schema(by_alias=True)


def test_cost_requires_honest_estimate_label():
    with pytest.raises(ValidationError, match="ESTIMATED"):
        ResourceUsage(estimated_cost_usd=0.1)
    usage = ResourceUsage(estimated_cost_usd=0.1, cost_confidence="ESTIMATED")
    assert usage.counter_confidence == "EXACT"
    assert usage.cost_confidence == "ESTIMATED"
