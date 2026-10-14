"""Async agent boundary and a deterministic, observable payments reference agent."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from worldforge.models import AgentAction, AgentDecision, AgentObservation


@runtime_checkable
class AgentAdapter(Protocol):
    """Trusted adapters receive only task-visible observations, never hidden objectives."""

    @property
    def metadata(self) -> dict[str, JsonValue]: ...

    async def reset(self) -> None: ...

    async def act(self, observation: AgentObservation) -> AgentDecision: ...

    async def shutdown(self) -> None: ...


class ReferenceAgent:
    """Discover the duplicate from tool output and refund it with a stable retry key.

    This is a narrow reference policy, not an LLM or a general planning system.
    It records no internal reasoning and needs no provider credentials.
    """

    def __init__(self) -> None:
        self._pending: AgentAction | None = None
        self._listed = False
        self._finished = False

    @property
    def metadata(self) -> dict[str, JsonValue]:
        return {"name": "reference", "version": "1", "deterministic": True}

    async def reset(self) -> None:
        self._pending = None
        self._listed = False
        self._finished = False

    async def shutdown(self) -> None:
        return None

    async def act(self, observation: AgentObservation) -> AgentDecision:
        if self._finished:
            return AgentDecision(message="Finished.")
        result = observation.last_result
        if self._pending is not None:
            if result is not None and result.status in (503, 504):
                # Reuse the entire action: changing the key could duplicate the effect.
                return AgentDecision(action=self._pending.model_copy(deep=True))
            if result is not None and 200 <= result.status < 300:
                self._finished = True
                return AgentDecision(message="The duplicate payment has been refunded.")
            self._finished = True
            return AgentDecision(message="The refund could not be completed.")
        if not self._listed:
            customer_id = observation.visible.get("customer_id")
            if not isinstance(customer_id, str) or "list_payments" not in observation.tools:
                self._finished = True
                return AgentDecision(message="The task lacks a supported payment lookup.")
            self._listed = True
            return AgentDecision(
                action=AgentAction(tool="list_payments", arguments={"customer_id": customer_id})
            )
        if result is not None and result.status in (503, 504):
            return AgentDecision(
                action=AgentAction(
                    tool="list_payments",
                    arguments={"customer_id": observation.visible.get("customer_id")},
                )
            )
        records = result.data.get("data") if result and isinstance(result.data, dict) else None
        if isinstance(records, list):
            duplicates = [
                record
                for record in records
                if isinstance(record, dict)
                and isinstance(record.get("duplicate_of"), str)
                and isinstance(record.get("id"), str)
                and isinstance(record.get("amount"), int)
                and not isinstance(record.get("amount"), bool)
                and record.get("refunded_amount", 0) == 0
            ]
            refund_tools = sorted(
                name for name, tool in observation.tools.items() if tool.kind == "refund"
            )
            if len(duplicates) == 1 and len(refund_tools) == 1:
                duplicate = duplicates[0]
                identifier = duplicate["id"]
                self._pending = AgentAction(
                    tool=refund_tools[0],
                    arguments={
                        "payment_id": identifier,
                        "amount": duplicate["amount"],
                        "idempotency_key": f"worldforge-reference-refund-{identifier}",
                    },
                )
                return AgentDecision(action=self._pending.model_copy(deep=True))
        self._finished = True
        return AgentDecision(message="No unambiguous duplicate payment was found.")
