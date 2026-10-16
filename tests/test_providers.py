from __future__ import annotations

from pathlib import Path

from worldforge.agents import ReferenceAgent
from worldforge.compiler import compile_openapi
from worldforge.models import (
    PricingConfig,
    ProviderIdentity,
    ProviderRecord,
    ProviderRequest,
    RateLimitMetadata,
)
from worldforge.providers import MockProvider, ProviderAgentAdapter
from worldforge.runtime import replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios

EXAMPLE = Path(__file__).parents[1] / "examples/payments/openapi.yaml"


def test_mock_provider_needs_no_credentials_and_reports_actual_requests(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42, response_lost=True)[0]
    adapter = ProviderAgentAdapter(MockProvider())
    run = run_scenario_sync(world, scenario, adapter)
    assert run.evaluation.success
    assert run.resource_report is not None
    assert run.resource_report.metrics["provider_requests"].value == 4
    assert run.resource_report.metrics["input_tokens"].value is None
    assert len(run.resource_report.providers) == 4
    assert adapter.drain_provider_records() == []
    assert replay_run(run)


def test_explicit_mock_fixture_usage_pricing_and_quota_roundtrip() -> None:
    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    quota = RateLimitMetadata(limit=100, remaining=98, reset_after_ms=1000)
    adapter = ProviderAgentAdapter(
        MockProvider(input_tokens=10, output_tokens=2, rate_limit=quota),
        pricing=PricingConfig(input_usd_per_million=2, output_usd_per_million=4),
    )
    run = run_scenario_sync(world, scenario, adapter)
    assert run.resource_report is not None
    assert run.resource_report.metrics["total_tokens"].value == 36
    assert run.resource_report.metrics["estimated_cost_usd"].value == 0.000084
    assert run.resource_report.providers[0].usage.rate_limit == quota
    assert run.agent_metadata["provider_kind"] == "mock"
    assert replay_run(run)


def test_provider_identity_mismatch_is_failed_run_with_cleanup() -> None:
    class BadProvider(MockProvider):
        shutdown_called = False

        async def complete(self, request: ProviderRequest):  # type: ignore[no-untyped-def]
            response = await super().complete(request)
            response.record.identity = ProviderIdentity(provider="unexpected", model="x")
            return response

        async def shutdown(self) -> None:
            self.shutdown_called = True

    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    provider = BadProvider()
    run = run_scenario_sync(world, scenario, ProviderAgentAdapter(provider))
    assert not run.evaluation.success
    assert run.evaluation.termination == "agent_act_error"
    assert provider.shutdown_called
    assert replay_run(run)


def test_provider_record_budget_returns_failed_artifact_instead_of_crashing() -> None:
    class ManyRecordsAgent(ReferenceAgent):
        def drain_provider_records(self) -> list[ProviderRecord]:
            return [
                ProviderRecord(identity=ProviderIdentity(provider="local", model="fixture"))
                for _ in range(1000)
            ]

    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    run = run_scenario_sync(world, scenario, ManyRecordsAgent())
    assert run.evaluation.termination == "agent_act_error"
    assert run.evaluation.objective_completed
    assert not run.evaluation.success
    assert run.resource_report is not None
    assert len(run.resource_report.providers) == 2000
    assert replay_run(run)


def test_provider_failure_keeps_partial_receipts_but_never_claims_complete_usage() -> None:
    class FailAfterFirstRequest(MockProvider):
        requests = 0

        async def complete(self, request: ProviderRequest):  # type: ignore[no-untyped-def]
            self.requests += 1
            if self.requests > 1:
                raise RuntimeError("provider failed after request dispatch")
            return await super().complete(request)

    world = compile_openapi(EXAMPLE)
    scenario = generate_scenarios(world, count=1, seed=42)[0]
    run = run_scenario_sync(world, scenario, ProviderAgentAdapter(FailAfterFirstRequest()))
    assert run.evaluation.termination == "agent_act_error"
    assert len(run.resource_report.providers) == 1
    assert run.resource_report.providers[0].usage.requests == 1
    assert run.resource_report.metrics["provider_requests"].value is None
    assert run.resource_report.metrics["provider_requests"].provenance == "UNAVAILABLE"
    assert replay_run(run)
