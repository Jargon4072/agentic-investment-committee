"""
agents/base_agent.py
====================
Base execution layer for every analyst agent in the Investment Committee.

Public surface
--------------
AgentConfig   – frozen dataclass carrying agent identity + system prompt
BaseAgent     – async argue() loop with prompt building, parse, and retry
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from models.schemas import AgentArgument, Verdict
from providers.base import LLMProvider

logger = logging.getLogger(__name__)

class AgentResponseSchema(BaseModel):
    """Schema forced on the LLM to guarantee valid output structure."""
    verdict: Verdict
    conviction_score: int = Field(ge=0, le=100)
    primary_argument: str
    supporting_points: List[str]
    key_risks_to_your_view: List[str]
    what_would_change_my_mind: str
    disagreement_with: Dict[str, str]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_RETRIES: int = 2
_RETRY_SUFFIX: str = (
    "\n\nYour previous response was not valid JSON. "
    "Return ONLY the JSON object."
)


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentConfig:
    """Immutable identity + system prompt for one analyst agent.

    Fields
    ------
    id:
        Stable machine identifier, e.g. ``"warren"``.
    name:
        Short human name used inside prompts, e.g. ``"Warren"``.
    lens:
        Analytical lens label, e.g. ``"value"``.
    system_prompt:
        The full persona system prompt sent to the LLM.
    display_name:
        Rich display name shown in CLI/UI, e.g. ``"Warren · Value Investor"``.
    """

    id: str
    name: str
    lens: str
    system_prompt: str
    display_name: str


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class BaseAgent:
    """Execution wrapper around one LLM provider call per debate round.

    Concrete subclasses only need to call ``super().__init__(config, provider)``
    with the correct :class:`AgentConfig`.  All persona behaviour lives in the
    system prompt.

    Parameters
    ----------
    config:
        Agent identity and system prompt.
    provider:
        The live :class:`~providers.base.LLMProvider` to call.
    """

    def __init__(self, config: AgentConfig, provider: LLMProvider) -> None:
        self.config = config
        self.provider = provider
        self._id = config.id
        self._name = config.name
        self._lens = config.lens

        logger.debug(
            "[agent] Initialised %s (lens=%s)", config.display_name, config.lens
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def argue(
        self,
        thesis: str,
        round_num: int,
        prior_arguments: List[AgentArgument],
        max_tokens: int,
    ) -> AgentArgument:
        """Produce one :class:`~models.schemas.AgentArgument` for this round.

        Builds a structured prompt from the thesis + prior arguments, calls
        the LLM provider, and parses the JSON response.  Retries up to
        ``_MAX_RETRIES`` times on parse failure before returning a sentinel
        error argument.

        Parameters
        ----------
        thesis:
            The investment thesis being debated.
        round_num:
            Current 1-indexed debate round.
        prior_arguments:
            All :class:`AgentArgument` objects produced in previous rounds
            (may be empty for round 1).
        max_tokens:
            Hard token ceiling passed directly to the provider.

        Returns
        -------
        AgentArgument
            Always returns — never raises.  On persistent failure, returns
            an argument with ``error`` set and safe defaults
            (``verdict=HOLD``, ``conviction_score=50``).
        """
        prompt = self._build_prompt(thesis, round_num, prior_arguments)
        raw: str = ""
        last_error: Optional[str] = None

        for attempt in range(1 + _MAX_RETRIES):
            effective_prompt = prompt if attempt == 0 else prompt + _RETRY_SUFFIX

            try:
                response = await self.provider.complete(
                    prompt=effective_prompt,
                    max_tokens=max_tokens,
                    system=self.config.system_prompt,
                    temperature=0.7,
                    response_schema=AgentResponseSchema,
                )
                raw = response.text
                tokens_used = response.tokens_used

            except Exception as exc:
                last_error = f"Provider error on attempt {attempt + 1}: {exc}"
                logger.warning(
                    "[agent:%s] provider error round=%d attempt=%d: %s",
                    self._id, round_num, attempt + 1, exc,
                )
                # Don't retry on provider errors — the provider layer
                # already handles its own error semantics.
                break

            argument = self._parse_response(
                raw=raw,
                round_num=round_num,
                tokens_used=tokens_used,
            )

            if argument.error is None:
                logger.info(
                    "[agent:%s] round=%d verdict=%s conviction=%d tokens=%d",
                    self._id,
                    round_num,
                    argument.verdict.value,
                    argument.conviction_score,
                    argument.tokens_used,
                )
                return argument

            last_error = argument.error
            logger.warning(
                "[agent:%s] parse failure round=%d attempt=%d: %s",
                self._id, round_num, attempt + 1, last_error,
            )

        # All retries exhausted — return a safe sentinel
        logger.error(
            "[agent:%s] all retries exhausted round=%d — returning error sentinel",
            self._id, round_num,
        )
        return self._error_argument(round_num=round_num, error=last_error or "Unknown error")

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        thesis: str,
        round_num: int,
        prior_arguments: List[AgentArgument],
    ) -> str:
        """Build the user-turn prompt for this agent.

        Prior arguments are injected as a human-readable structured summary,
        NOT raw JSON, so the LLM can focus on analytical content rather than
        parsing artefacts.

        Format per prior argument::

            [Round X] AgentName (lens): VERDICT (score/100)
            Key argument: <primary_argument>
            Their concern about you: <disagreement_with[self.id] or None>

        Parameters
        ----------
        thesis:
            The investment thesis being debated.
        round_num:
            Current 1-indexed round number.
        prior_arguments:
            Arguments from all previous rounds, all agents.

        Returns
        -------
        str
            The fully assembled prompt string.
        """
        lines: List[str] = []

        lines.append(f"INVESTMENT THESIS: {thesis}")
        lines.append(f"You are in Round {round_num} of the debate.")
        lines.append("")

        if prior_arguments:
            lines.append("── Prior Committee Arguments ──────────────────────────────")
            for arg in prior_arguments:
                concern = arg.disagreement_with.get(self._id) or arg.disagreement_with.get(self._name)
                lines.append(
                    f"[Round {arg.round_num}] {arg.agent_name} ({arg.lens}): "
                    f"{arg.verdict.value} ({arg.conviction_score}/100)"
                )
                lines.append(f"  Key argument: {arg.primary_argument}")
                if concern:
                    lines.append(f"  Their concern about you: {concern}")
                lines.append("")
            lines.append("────────────────────────────────────────────────────────────")
            lines.append("")

        if round_num == 1:
            lines.append(
                "This is the opening round. Provide your independent assessment "
                "of the thesis from your analytical lens. Do not manufacture "
                "disagreements — only flag real ones."
            )
        else:
            lines.append(
                "You have read the prior arguments above. "
                "Update your position if any argument genuinely moved you. "
                "Engage specifically with the argument that most challenges your view. "
                "Do not change your position simply to agree — only if the logic is sound."
            )

        lines.append("")
        lines.append(
            "Respond with ONLY a valid JSON object. "
            "No preamble. No markdown fences. No explanation outside the JSON."
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        raw: str,
        round_num: int,
        tokens_used: int = 0,
    ) -> AgentArgument:
        """Parse the raw LLM response into an :class:`AgentArgument`.

        Delegates to :meth:`AgentArgument.parse_llm_response` which handles
        markdown fence stripping and field-name aliasing.

        On any parse/validation failure, returns an AgentArgument with
        ``error`` set so the caller can decide to retry.

        Parameters
        ----------
        raw:
            The raw text from the LLM response.
        round_num:
            Current round number — injected as an override.
        tokens_used:
            Token count from the provider response.

        Returns
        -------
        AgentArgument
            Parsed argument on success; error sentinel on failure.
        """
        try:
            return AgentArgument.parse_llm_response(
                raw,
                agent_id=self._id,
                agent_name=self.config.display_name,
                lens=self._lens,
                round_num=round_num,
                tokens_used=tokens_used,
            )
        except Exception as exc:
            # Log the raw response so we can diagnose field-name drift
            logger.warning(
                "[agent:%s] parse error: %s\nRAW RESPONSE (first 500 chars):\n%s",
                self._id, exc, raw[:500],
            )
            return self._error_argument(
                round_num=round_num,
                error=f"Parse failed: {exc}",
                tokens_used=tokens_used,
            )


    # ------------------------------------------------------------------
    # Error sentinel factory
    # ------------------------------------------------------------------

    def _error_argument(
        self,
        round_num: int,
        error: str,
        tokens_used: int = 0,
    ) -> AgentArgument:
        """Return a safe default AgentArgument carrying an error description."""
        return AgentArgument(
            agent_id=self._id,
            agent_name=self.config.display_name,
            lens=self._lens,
            round_num=round_num,
            verdict=Verdict.HOLD,
            conviction_score=50,
            primary_argument="[Agent failed to produce a valid response]",
            what_would_change_my_mind="[N/A — response error]",
            tokens_used=tokens_used,
            timestamp=datetime.utcnow(),
            error=error,
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"id={self._id!r}, lens={self._lens!r}, "
            f"provider={self.provider.provider_name!r})"
        )
