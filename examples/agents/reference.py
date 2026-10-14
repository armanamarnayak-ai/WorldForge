"""Trusted Python adapter factory for the built-in deterministic reference agent."""

from worldforge.agents import AgentAdapter, ReferenceAgent


def create_agent() -> AgentAdapter:
    """Return a fresh adapter; the runtime owns its lifecycle for a scenario."""
    return ReferenceAgent()
