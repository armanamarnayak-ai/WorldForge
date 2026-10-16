import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from worldforge.models import (
    FailureCertificate,
    ResourceReport,
    ResourceUsage,
    Run,
    Scenario,
    SearchSpec,
    WorldDefinition,
)


@pytest.mark.parametrize(
    "name,version,model",
    [
        ("world", "1.1", WorldDefinition),
        ("scenario", "1.1", Scenario),
        ("run", "1.1", Run),
        ("failure-certificate", "1.0", FailureCertificate),
        ("resource-report", "1.0", ResourceReport),
        ("search", "1.0", SearchSpec),
    ],
)
def test_checked_in_schemas_match_public_contract(name, version, model):
    path = Path(__file__).resolve().parents[1] / "schemas" / f"{name}-{version}.json"
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    schema.pop("$id")
    schema.pop("$schema")
    assert schema == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize("fixture", ["v0.1-run.json", "v0.1-unsafe-run.json"])
def test_released_artifacts_validate_against_frozen_schemas(fixture):
    root = Path(__file__).resolve().parents[1]
    run = json.loads((root / "tests" / "fixtures" / fixture).read_text())
    for name, artifact in [("run", run), ("world", run["world"]), ("scenario", run["scenario"])]:
        schema = json.loads((root / "schemas" / f"{name}-1.0.json").read_text())
        Draft202012Validator(schema).validate(artifact)


def test_cost_requires_honest_estimate_label():
    with pytest.raises(ValidationError, match="ESTIMATED"):
        ResourceUsage(estimated_cost_usd=0.1)
    usage = ResourceUsage(estimated_cost_usd=0.1, cost_confidence="ESTIMATED")
    assert usage.counter_confidence == "EXACT"
    assert usage.cost_confidence == "ESTIMATED"
