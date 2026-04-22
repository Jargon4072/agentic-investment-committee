"""agents/tiebreaker_agent.py — Solomon, the committee chair / tie-breaker.

Solomon is spawned on-demand by the orchestrator when the committee reaches
irreconcilable disagreement.  He is never a standing panelist.

His output is still an AgentArgument (same schema as every other agent) but
his system prompt instructs him to classify the disagreement type, identify
the crux, and make a ruling — extra fields he includes are stripped by
AgentArgument.parse_llm_response (unknown-field handling) and the orchestrator
reads the primary_argument for the ruling text.
"""

from __future__ import annotations

from providers.base import LLMProvider
from .base_agent import AgentConfig, BaseAgent
from .prompts import SOLOMON_SYSTEM_PROMPT


class TiebreakerAgent(BaseAgent):
    """Solomon · Committee Chair.

    Lens: committee-level conflict classification and ruling.
    Persona lives entirely in :data:`SOLOMON_SYSTEM_PROMPT`.

    Unlike the analyst agents, Solomon is typically called with a custom
    prompt assembled by the orchestrator that describes the specific conflict.
    The :meth:`~agents.base_agent.BaseAgent.argue` method still works normally;
    the orchestrator can pass the conflict context as the ``thesis`` argument
    or via the ``prior_arguments`` list.
    """

    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(
            config=AgentConfig(
                id="solomon",
                name="Solomon",
                lens="committee",
                system_prompt=SOLOMON_SYSTEM_PROMPT,
                display_name="Solomon · Committee Chair",
            ),
            provider=provider,
        )
