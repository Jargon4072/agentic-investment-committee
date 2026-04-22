"""agents/macro_agent.py — Ray, the global macro strategist."""

from __future__ import annotations

from providers.base import LLMProvider
from .base_agent import AgentConfig, BaseAgent
from .prompts import RAY_SYSTEM_PROMPT


class MacroAgent(BaseAgent):
    """Ray · Macro Strategist.

    Lens: credit cycles, liquidity regimes, geopolitical risk surface.
    Persona lives entirely in :data:`RAY_SYSTEM_PROMPT`.
    """

    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(
            config=AgentConfig(
                id="ray",
                name="Ray",
                lens="macro",
                system_prompt=RAY_SYSTEM_PROMPT,
                display_name="Ray · Macro Strategist",
            ),
            provider=provider,
        )
