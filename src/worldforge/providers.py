"""Optional provider-neutral model boundary, with a local deterministic CI implementation.

Providers return observable decisions and typed usage only. This module never executes
production APIs, discovers credentials, or guesses token counts or pricing.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

from worldforge.agents import ReferenceAgent
from worldforge.models import (
    AgentDecision,
    AgentObservation,
    PricingConfig,
    ProviderIdentity,
    ProviderRecord,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    RateLimitMetadata,
)


class ModelProvider(Protocol):
    """Trusted implementations may wrap a provider, but CI uses only local providers."""

    @property
    def identity(self) -> ProviderIdentity: ...

    async def reset(self) -> None: ...

    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    async def shutdown(self) -> None: ...


class MockProvider:
    """Local reference policy; optional token numbers are explicitly fixture reports.

    The default reports only the actual number of complete() requests. Token usage is
    unavailable unless a caller intentionally supplies a mock usage fixture.
    """

    def __init__(
        self,
        *,
        model: str = "reference-policy",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        rate_limit: RateLimitMetadata | None = None,
    ) -> None:
        self._identity = ProviderIdentity(provider="local", model=model, kind="mock")
        self._usage = ProviderUsage(
            requests=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            rate_limit=rate_limit,
        )
        self._policy = ReferenceAgent()

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity.model_copy(deep=True)

    async def reset(self) -> None:
        await self._policy.reset()

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            decision=await self._policy.act(request.observation),
            record=ProviderRecord(identity=self.identity, usage=self._usage.model_copy(deep=True)),
        )

    async def shutdown(self) -> None:
        await self._policy.shutdown()


class ProviderAgentAdapter:
    """Adapt model decisions to AgentAdapter and drain per-request observable usage.

    The runtime bounds callbacks, validates returned decisions and records, and never
    persists arbitrary request headers, credential values, or provider raw responses.
    Provider code itself is trusted Python and is not sandboxed.
    """

    def __init__(self, provider: ModelProvider, *, pricing: PricingConfig | None = None) -> None:
        self.provider = provider
        self.pricing = pricing.model_copy(deep=True) if pricing is not None else None
        self._records: list[ProviderRecord] = []
        self._requests = 0

    @property
    def metadata(self) -> dict[str, JsonValue]:
        identity = self.provider.identity
        return {
            "name": "provider",
            "provider": identity.provider,
            "model": identity.model,
            "provider_kind": identity.kind,
            "deterministic": identity.kind == "mock",
            "usage_source": "adapter reported; mock token values are explicit fixtures",
        }

    async def reset(self) -> None:
        self._records.clear()
        self._requests = 0
        await self.provider.reset()

    async def act(self, observation: AgentObservation) -> AgentDecision:
        self._requests += 1
        reply = await self.provider.complete(
            ProviderRequest(
                observation=observation,
                request_metadata={"request_sequence": self._requests},
            )
        )
        if not isinstance(reply, ProviderResponse):
            raise TypeError("provider complete must return ProviderResponse")
        record = ProviderRecord.model_validate(
            reply.record.model_dump(mode="python", warnings="error")
        )
        if record.identity != self.provider.identity:
            raise ValueError("provider response identity does not match configured identity")
        if self.pricing is not None:
            record.pricing = self.pricing.model_copy(deep=True)
        self._records.append(record)
        return reply.decision

    def drain_provider_records(self) -> list[ProviderRecord]:
        records, self._records = self._records, []
        return records

    async def shutdown(self) -> None:
        await self.provider.shutdown()
