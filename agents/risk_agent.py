"""agents/risk_agent.py — Nassim, the tail-risk and convexity specialist."""

from __future__ import annotations

from providers.base import LLMProvider
from .base_agent import AgentConfig, BaseAgent
from .prompts import NASSIM_SYSTEM_PROMPT


class RiskAgent(BaseAgent):
    """Nassim · Risk Manager.

    Lens: fat tails, convexity, fragility, asymmetric payoffs.
    Persona lives entirely in :data:`NASSIM_SYSTEM_PROMPT`.
    """

    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(
            config=AgentConfig(
                id="nassim",
                name="Nassim",
                lens="risk",
                system_prompt=NASSIM_SYSTEM_PROMPT,
                display_name="Nassim · Risk Manager",
            ),
            provider=provider,
        )
