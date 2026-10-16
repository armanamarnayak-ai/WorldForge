"""Trusted local demo adapter; deliberately changes keys after an ambiguous refund."""

from worldforge.agents import VulnerableRefundAgent


def create_agent() -> VulnerableRefundAgent:
    return VulnerableRefundAgent()
