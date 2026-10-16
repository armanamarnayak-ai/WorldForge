from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from worldforge.models import (
    AgentAction,
    PricingConfig,
    ProviderIdentity,
    ProviderRecord,
    ProviderUsage,
    RateLimitMetadata,
    ResourceReport,
    ToolResult,
)
from worldforge.resources import aggregate_reports, resource_report


def report(
    calls: list[tuple[AgentAction, ToolResult]], providers: list[ProviderRecord] | None = None
) -> ResourceReport:
    return resource_report(
        calls,
        world_steps=len(calls),
        max_steps=12,
        simulated_latency_ms=75,
        elapsed_seconds=0.1,
        providers=providers,
    )


def test_counts_and_unavailable_values_are_honest() -> None:
    action = AgentAction(tool="lookup", arguments={"id": "a"})
    result = report([(action, ToolResult(status=503)), (action, ToolResult(status=200))])
    assert result.metrics["tool_calls"].value == 2
    assert result.metrics["successful_calls"].value == 1
    assert result.metrics["failed_calls"].value == 1
    assert result.metrics["retries"].value == 1
    assert result.metrics["duplicate_calls"].value == 1
    assert result.metrics["simulated_latency_ms"].value == 75
    assert result.metrics["actual_runtime_latency_ms"].value == 100
    for name in ("provider_requests", "input_tokens", "total_tokens", "estimated_cost_usd"):
        assert result.metrics[name].value is None
        assert result.metrics[name].provenance == "UNAVAILABLE"


def test_changed_retry_keys_still_count_as_retry_not_identical_duplicate() -> None:
    calls = [
        (
            AgentAction(tool="refund", arguments={"payment_id": "p", "idempotency_key": key}),
            ToolResult(status=status),
        )
        for key, status in [("stable", 504), ("changed", 409)]
    ]
    result = report(calls)
    assert result.metrics["retries"].value == 1
    assert result.metrics["duplicate_calls"].value == 0


def test_tokens_cost_and_reported_provenance_require_explicit_information() -> None:
    record = ProviderRecord(
        identity=ProviderIdentity(provider="local", model="fixture", kind="mock"),
        usage=ProviderUsage(requests=1, input_tokens=100, output_tokens=20),
    )
    unpriced = report([], [record])
    assert unpriced.metrics["provider_requests"].value == 1
    assert unpriced.metrics["input_tokens"].value == 100
    assert unpriced.metrics["total_tokens"].value == 120
    assert unpriced.metrics["estimated_cost_usd"].value is None
    record.pricing = PricingConfig(input_usd_per_million=2, output_usd_per_million=10)
    priced = report([], [record, record])
    assert priced.metrics["estimated_cost_usd"].value == pytest.approx(0.0008)
    assert priced.metrics["estimated_cost_usd"].provenance == "ESTIMATED"
    assert priced.metrics["input_tokens"].provenance == "CALCULATED"
    missing = record.model_copy(deep=True)
    missing.usage.output_tokens = None
    incomplete = report([], [record, missing])
    assert incomplete.metrics["input_tokens"].value == 200
    assert incomplete.metrics["output_tokens"].value is None
    assert incomplete.metrics["total_tokens"].value is None
    assert incomplete.metrics["estimated_cost_usd"].value is None


def test_warning_thresholds_are_observable_and_aggregate_is_bounded() -> None:
    action = AgentAction(tool="lookup")
    calls = [(action, ToolResult(status=503))] * 10
    result = report(calls)
    assert {warning.code for warning in result.warnings} == {
        "retry_loop",
        "excessive_repeated_calls",
        "nearing_step_budget",
    }
    record = ProviderRecord(identity=ProviderIdentity(provider="local", model="mock"))
    result.providers = [record] * 2000
    aggregate = aggregate_reports([result, result])
    assert aggregate.metrics["tool_calls"].value == 20
    assert aggregate.metrics["input_tokens"].value is None
    assert aggregate.providers == []
    assert len(aggregate.warnings) == 3
    assert aggregate_reports([]).metrics == {}


@given(st.lists(st.integers(min_value=200, max_value=599), max_size=50))
def test_status_accounting_conserves_calls(statuses: list[int]) -> None:
    result = report(
        [(AgentAction(tool="lookup"), ToolResult(status=status)) for status in statuses]
    )
    assert result.metrics["successful_calls"].value + result.metrics["failed_calls"].value == len(
        statuses
    )
    assert result.metrics["duplicate_calls"].value == max(0, len(statuses) - 1)


def test_aggregate_rejects_different_units() -> None:
    first, second = report([]), report([])
    second.metrics["tool_calls"].unit = "seconds"
    with pytest.raises(ValueError, match="units"):
        aggregate_reports([first, second])


def test_quota_warning_uses_reported_metadata_without_inventing_remaining_usage() -> None:
    record = ProviderRecord(
        identity=ProviderIdentity(provider="local", model="fixture", kind="mock"),
        usage=ProviderUsage(rate_limit=RateLimitMetadata(limit=100, remaining=20)),
    )
    result = report([], [record])
    assert [warning.code for warning in result.warnings] == ["nearing_reported_provider_quota"]
    assert result.providers[0].usage.rate_limit.remaining == 20
    assert result.metrics["provider_requests"].value is None
