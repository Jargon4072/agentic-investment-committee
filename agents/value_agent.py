"""agents/value_agent.py — Warren, the deep value investor."""

from __future__ import annotations

from providers.base import LLMProvider
from .base_agent import AgentConfig, BaseAgent
from .prompts import WARREN_SYSTEM_PROMPT


class ValueAgent(BaseAgent):
    """Warren · Value Investor.

    Lens: DCF, FCF yield, margin of safety, balance sheet strength.
    Persona lives entirely in :data:`WARREN_SYSTEM_PROMPT`.
    """

    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(
            config=AgentConfig(
                id="warren",
                name="Warren",
                lens="value",
                system_prompt=WARREN_SYSTEM_PROMPT,
                display_name="Warren · Value Investor",
            ),
            provider=provider,
        )
