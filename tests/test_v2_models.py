import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from worldforge.models import (
    FailureSpec,
    Measurement,
    PricingConfig,
    ProviderUsage,
    RateLimitMetadata,
    Run,
    Scenario,
    SearchSpec,
)


@pytest.mark.parametrize("fixture", ["v0.1-run", "v0.1-unsafe-run"])
def test_real_legacy_run_preserves_wire_format(fixture):
    path = Path(__file__).parent / "fixtures" / f"{fixture}.json"
    original = json.loads(path.read_text())
    restored = Run.model_validate(original)
    assert restored.schema_version == "1.0"
    assert restored.resource_report is None
    assert json.loads(restored.model_dump_json()) == original


def test_extended_scenario_requires_new_version():
    data = json.loads((Path(__file__).parent / "fixtures/v0.1-run.json").read_text())["scenario"]
    data["failures"] = [FailureSpec(action="create_refund", kind="rate_limit").model_dump()]
    with pytest.raises(ValidationError, match="schema_version 1.1"):
        Scenario.model_validate(data)
    data["schema_version"] = "1.1"
    scenario = Scenario.model_validate(data)
    assert isinstance(scenario.failures[0], FailureSpec)
    assert Scenario.model_validate_json(scenario.model_dump_json()) == scenario


@pytest.mark.parametrize(
    "value,provenance",
    [
        (None, "EXACT"),
        (1, "UNAVAILABLE"),
        (True, "EXACT"),
        (float("inf"), "ESTIMATED"),
        (-1, "CALCULATED"),
    ],
)
def test_measurements_reject_invented_or_invalid_values(value, provenance):
    with pytest.raises(ValidationError):
        Measurement(value=value, provenance=provenance, source="test", unit="calls")


def test_missing_provider_usage_is_unavailable_not_zero():
    assert ProviderUsage().requests is None
    assert ProviderUsage().input_tokens is None
    with pytest.raises(ValidationError):
        ProviderUsage(requests=True)
    with pytest.raises(ValidationError):
        PricingConfig(input_usd_per_million=float("nan"), output_usd_per_million=1)
    with pytest.raises(ValidationError):
        RateLimitMetadata(limit=10, remaining=11)


def test_search_inputs_and_budgets_are_bounded():
    with pytest.raises(ValidationError):
        SearchSpec(max_runs=0)
    with pytest.raises(ValidationError):
        SearchSpec(conditions=[FailureSpec(action="tool", kind="delay")] * 9)
    with pytest.raises(ValidationError, match="unique"):
        SearchSpec(seeds=[1, 1])
    with pytest.raises(ValidationError):
        FailureSpec(action="tool", kind="delay", latency_ms=60001)


@pytest.mark.parametrize("seed", [True, "42", 42.0])
def test_search_seed_requires_a_declared_integer(seed):
    with pytest.raises(ValidationError):
        SearchSpec(seeds=[seed])
