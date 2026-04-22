"""
agents/factory.py
=================
Builds all analyst agents with a shared, injected :class:`LLMProvider`.

Public surface
--------------
build_panel(provider)      → the 4 standing analyst agents
build_tiebreaker(provider) → Solomon, spawned on demand
build_all(provider)        → full panel + tiebreaker (useful for tests)
"""

from __future__ import annotations

from typing import Dict, List

from providers.base import LLMProvider

from .base_agent import BaseAgent
from .growth_agent import GrowthAgent
from .macro_agent import MacroAgent
from .risk_agent import RiskAgent
from .tiebreaker_agent import TiebreakerAgent
from .value_agent import ValueAgent


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def build_panel(provider: LLMProvider) -> List[BaseAgent]:
    """Return the four standing analyst agents in canonical debate order.

    Order: Warren → Cathie → Ray → Nassim
    The order is intentional: value/growth contrast leads, macro/risk follow.

    Parameters
    ----------
    provider:
        A live :class:`~providers.base.LLMProvider` shared across all agents.
        Sharing one provider instance avoids re-initialising SDK clients.

    Returns
    -------
    list[BaseAgent]
        [ValueAgent, GrowthAgent, MacroAgent, RiskAgent]
    """
    return [
        ValueAgent(provider),
        GrowthAgent(provider),
        MacroAgent(provider),
        RiskAgent(provider),
    ]


def build_tiebreaker(provider: LLMProvider) -> TiebreakerAgent:
    """Return Solomon, the on-demand tie-breaker agent.

    Solomon is NOT part of the standing panel.  The orchestrator spawns him
    only when the committee reaches irreconcilable disagreement.

    Parameters
    ----------
    provider:
        May be the same provider instance used for the panel, or a separate
        one with a higher-capability model if the orchestrator so chooses.
    """
    return TiebreakerAgent(provider)


def build_all(provider: LLMProvider) -> Dict[str, BaseAgent]:
    """Return a dict of all agents keyed by their stable ``agent_id``.

    Useful for tests and introspection.  Includes the tiebreaker.

    Returns
    -------
    dict[str, BaseAgent]
        Keys: ``"warren"``, ``"cathie"``, ``"ray"``, ``"nassim"``, ``"solomon"``
    """
    agents: List[BaseAgent] = build_panel(provider) + [build_tiebreaker(provider)]
    return {agent.config.id: agent for agent in agents}