"""Measured resource summaries with explicit availability and calculation provenance."""

from __future__ import annotations

import json
from collections import Counter

from worldforge.models import (
    AgentAction,
    Measurement,
    MeasurementProvenance,
    ProviderRecord,
    ResourceReport,
    ResourceWarning,
    ToolResult,
)


def _identity(action: AgentAction, *, retry: bool = False) -> str:
    arguments = dict(action.arguments)
    if retry:
        arguments.pop("idempotency_key", None)
    return json.dumps([action.tool, arguments], sort_keys=True, separators=(",", ":"))


def retry_count(calls: list[tuple[AgentAction, ToolResult]]) -> int:
    """Count consecutive retries of a failed operation, including changed retry keys."""
    return sum(
        result.status >= 400 and _identity(before, retry=True) == _identity(after, retry=True)
        for (before, result), (after, _) in zip(calls, calls[1:], strict=False)
    )


def _measurement(value: int | float | None, provenance: str, source: str, unit: str) -> Measurement:
    return Measurement(
        value=value, provenance=MeasurementProvenance(provenance), source=source, unit=unit
    )


def _reported_sum(records: list[ProviderRecord], field: str) -> int | None:
    if not records:
        return None
    values: list[int | None] = [
        record.usage.requests
        if field == "requests"
        else record.usage.input_tokens
        if field == "input_tokens"
        else record.usage.output_tokens
        for record in records
    ]
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def resource_report(
    calls: list[tuple[AgentAction, ToolResult]],
    *,
    world_steps: int,
    max_steps: int,
    simulated_latency_ms: int,
    elapsed_seconds: float,
    providers: list[ProviderRecord] | None = None,
    provider_usage_complete: bool = True,
) -> ResourceReport:
    """Build a report from runtime counters and explicitly reported provider usage.

    Provider numbers are adapter reports, not independent provider-side verification.
    No records, incomplete token reports, or absent pricing remain unavailable.
    """
    records = [record.model_copy(deep=True) for record in providers or []]
    calls_count = len(calls)
    failed = sum(result.status >= 400 for _, result in calls)
    identities = Counter(_identity(action) for action, _ in calls)
    duplicates = sum(count - 1 for count in identities.values())
    retries = retry_count(calls)
    metrics = {
        "tool_calls": _measurement(calls_count, "EXACT", "runtime action counter", "calls"),
        "successful_calls": _measurement(
            calls_count - failed, "CALCULATED", "tool calls minus failed status codes", "calls"
        ),
        "failed_calls": _measurement(failed, "EXACT", "tool status >= 400 counter", "calls"),
        "retries": _measurement(
            retries, "CALCULATED", "consecutive same-operation calls after failure", "calls"
        ),
        "duplicate_calls": _measurement(
            duplicates, "CALCULATED", "repeated identical tool/argument pairs", "calls"
        ),
        "world_steps": _measurement(world_steps, "EXACT", "simulator logical step delta", "steps"),
        "simulated_latency_ms": _measurement(
            simulated_latency_ms, "EXACT", "simulator virtual clock", "ms"
        ),
        "actual_runtime_latency_ms": _measurement(
            elapsed_seconds * 1000, "EXACT", "runtime monotonic wall clock", "ms"
        ),
    }
    for name, field, unit in (
        ("provider_requests", "requests", "requests"),
        ("input_tokens", "input_tokens", "tokens"),
        ("output_tokens", "output_tokens", "tokens"),
    ):
        value = _reported_sum(records, field) if provider_usage_complete else None
        metrics[name] = _measurement(
            value,
            "CALCULATED" if value is not None else "UNAVAILABLE",
            (
                "sum of typed adapter-reported usage; not provider-side verification"
                if provider_usage_complete
                else "provider usage incomplete after adapter error or rejected decision"
            ),
            unit,
        )
    input_tokens = metrics["input_tokens"].value
    output_tokens = metrics["output_tokens"].value
    total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    metrics["total_tokens"] = _measurement(
        total,
        "CALCULATED" if total is not None else "UNAVAILABLE",
        "reported input plus output tokens",
        "tokens",
    )
    cost: float | None = 0.0 if records and provider_usage_complete else None
    for record in records:
        if (
            not provider_usage_complete
            or record.pricing is None
            or record.usage.input_tokens is None
            or record.usage.output_tokens is None
        ):
            cost = None
            break
        assert cost is not None
        cost += (
            record.usage.input_tokens * record.pricing.input_usd_per_million
            + record.usage.output_tokens * record.pricing.output_usd_per_million
        ) / 1_000_000
    metrics["estimated_cost_usd"] = _measurement(
        cost,
        "ESTIMATED" if cost is not None else "UNAVAILABLE",
        "reported tokens times explicitly configured per-million-token USD pricing",
        "USD",
    )
    warnings: list[ResourceWarning] = []
    if retries >= 3:
        warnings.append(
            ResourceWarning(code="retry_loop", message="At least three failed-operation retries.")
        )
    if calls_count >= 4 and duplicates * 2 >= calls_count:
        warnings.append(
            ResourceWarning(
                code="excessive_repeated_calls",
                message="At least half of four or more calls repeat identical tool arguments.",
            )
        )
    if world_steps >= 100:
        warnings.append(
            ResourceWarning(code="high_step_usage", message="At least 100 simulator steps used.")
        )
    if calls_count * 5 >= max_steps * 4:
        warnings.append(
            ResourceWarning(code="nearing_step_budget", message="At least 80% of tool budget used.")
        )
    if any(
        record.usage.rate_limit is not None
        and record.usage.rate_limit.limit is not None
        and record.usage.rate_limit.remaining is not None
        and record.usage.rate_limit.remaining * 5 <= record.usage.rate_limit.limit
        for record in records
    ):
        warnings.append(
            ResourceWarning(
                code="nearing_reported_provider_quota",
                message=(
                    "An adapter-reported quota has at most 20% remaining; "
                    "not independently verified."
                ),
            )
        )
    return ResourceReport(metrics=metrics, warnings=warnings, providers=records)


def aggregate_reports(reports: list[ResourceReport]) -> ResourceReport:
    """Sum compatible measurements; any missing member keeps that metric unavailable."""
    if not reports:
        return ResourceReport()
    metrics: dict[str, Measurement] = {}
    for name in sorted({name for report in reports for name in report.metrics}):
        items = [report.metrics.get(name) for report in reports]
        present = [item for item in items if item is not None]
        units = {item.unit for item in present}
        if len(units) != 1:
            raise ValueError(f"cannot aggregate inconsistent units for {name}")
        available = all(item is not None and item.value is not None for item in items)
        value = sum(item.value for item in present if item.value is not None) if available else None
        provenance = (
            "ESTIMATED"
            if available and any(item.provenance == "ESTIMATED" for item in present)
            else "CALCULATED"
            if available
            else "UNAVAILABLE"
        )
        metrics[name] = _measurement(
            value, provenance, f"sum of {len(reports)} run reports", next(iter(units))
        )
    warnings = {
        (warning.code, warning.message): warning
        for report in reports
        for warning in report.warnings
    }
    return ResourceReport(
        metrics=metrics,
        warnings=[warnings[key].model_copy(deep=True) for key in sorted(warnings)],
        # Aggregates contain calculated totals, not an unbounded concatenation of
        # per-request records. Original records remain available in each run artifact.
        providers=[],
    )
