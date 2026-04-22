"""agents/growth_agent.py — Cathie, the high-conviction growth investor."""

from __future__ import annotations

from providers.base import LLMProvider
from .base_agent import AgentConfig, BaseAgent
from .prompts import CATHIE_SYSTEM_PROMPT


class GrowthAgent(BaseAgent):
    """Cathie · Growth Optimist.

    Lens: disruptive TAM, S-curves, ecosystem compounding.
    Persona lives entirely in :data:`CATHIE_SYSTEM_PROMPT`.
    """

    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(
            config=AgentConfig(
                id="cathie",
                name="Cathie",
                lens="growth",
                system_prompt=CATHIE_SYSTEM_PROMPT,
                display_name="Cathie · Growth Optimist",
            ),
            provider=provider,
        )
