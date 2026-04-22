"""agents package — analyst agent execution layer."""

from .base_agent import AgentConfig, BaseAgent
from .factory import build_all, build_panel, build_tiebreaker
from .growth_agent import GrowthAgent
from .macro_agent import MacroAgent
from .risk_agent import RiskAgent
from .tiebreaker_agent import TiebreakerAgent
from .value_agent import ValueAgent

__all__ = [
    # Core abstractions
    "AgentConfig",
    "BaseAgent",
    # Concrete agents
    "ValueAgent",
    "GrowthAgent",
    "MacroAgent",
    "RiskAgent",
    "TiebreakerAgent",
    # Factory
    "build_panel",
    "build_tiebreaker",
    "build_all",
]
