"""
cli/state.py
=============
Dataclasses for the DebateDisplay UI state.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional

from models.schemas import CommitteeMemo, DisagreementRecord


@dataclass
class AgentState:
    """Per-agent display state."""
    name: str
    agent_id: str
    lens: str
    status: str = "waiting"          # "waiting" | "thinking" | "done" | "error"
    verdict: str = ""
    conviction_score: int = 0
    elapsed_ms: float = 0.0
    latest_snippet: str = ""


@dataclass
class DisplayState:
    """Full UI state passed to DebateDisplay.update()."""
    round_num: int = 0
    max_rounds: int = 3
    mode: str = "explore"            # "explore" | "exploit"
    agents: List[AgentState] = field(default_factory=list)
    disagreements: List[DisagreementRecord] = field(default_factory=list)
    tokens_spent: int = 0
    total_budget: int = 50_000
    alerts: List[str] = field(default_factory=list)
    convergence_score: float = 0.0
    mode_just_switched: bool = False

    # Synthesis phase
    synthesizer_status: str = "idle"         # "idle" | "running" | "complete"
    synthesizer_stream_text: str = ""
    final_memo: Optional[CommitteeMemo] = None
