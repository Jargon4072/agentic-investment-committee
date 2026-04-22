"""
orchestrator/debate_config.py
==============================
DebateConfig dataclass — all parameters for one debate run.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class DebateConfig:
    """All parameters for one debate run.

    Parameters
    ----------
    thesis:           The investment thesis being debated (required).
    token_budget:     Hard token ceiling for the entire debate.
    max_rounds:       Maximum debate rounds.
    min_round_budget: Floor for BudgetManager.can_afford_round().
    tiebreaker_budget: Tokens reserved for Solomon tie-breaker.
    provider:         Provider name passed to get_provider() factory.
    output_dir:       Directory where JSON traces are auto-saved.
    """
    thesis: str
    token_budget: int = 50_000
    max_rounds: int = 3
    min_round_budget: int = 2_000
    tiebreaker_budget: int = 3_000
    provider: str = "gemini"
    output_dir: str = "traces"

    def as_dict(self) -> dict:
        return {
            "thesis": self.thesis,
            "token_budget": self.token_budget,
            "max_rounds": self.max_rounds,
            "min_round_budget": self.min_round_budget,
            "tiebreaker_budget": self.tiebreaker_budget,
            "provider": self.provider,
            "output_dir": self.output_dir,
        }
