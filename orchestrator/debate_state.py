"""
orchestrator/debate_state.py
=============================
DebateState dataclass — streamed after every agent/round event.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional

from models.schemas import (
    AgentArgument, CommitteeMemo, DebateMode, DisagreementRecord, RoundSummary
)


@dataclass
class DebateState:
    """Snapshot emitted by DebateOrchestrator.stream() after each event.

    The Rich UI consumes this to update its live dashboard.
    """
    event: str                              # "agent_done" | "round_done" | "mode_transition"
                                            # | "disagreement" | "tiebreaker" | "synthesis_done"
    round_num: int = 0
    mode: DebateMode = DebateMode.EXPLORE
    convergence_score: float = 0.0
    tokens_remaining: int = 0
    latest_argument: Optional[AgentArgument] = None
    round_summary: Optional[RoundSummary] = None
    disagreements: List[DisagreementRecord] = field(default_factory=list)
    mode_transition_reason: Optional[str] = None
    is_final: bool = False                  # True on "synthesis_done"
    final_memo: Optional[CommitteeMemo] = None

