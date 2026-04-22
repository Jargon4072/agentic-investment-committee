"""
models/schemas.py
=================
Pydantic v2 schemas for the Investment Committee multi-agent debate system.

Models
------
1.  AgentArgument       — one agent's output per round
2.  DisagreementRecord  — tracked conflict between two agents
3.  BudgetAllocation    — per-agent, per-round token accounting
4.  BudgetSummary       — rolled-up token budget view
5.  RoundSummary        — all arguments + disagreements for one round
6.  AgentFinalPosition  — each agent's stance after all rounds
7.  CommitteeMemo       — final synthesis / committee output
8.  DebateTrace         — the persisted, full debate record
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Verdict(str, Enum):
    """Investment recommendation verdict."""

    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG SELL"


class ConflictType(str, Enum):
    """Classification of disagreement between agents."""

    FACTUAL = "FACTUAL"
    METHODOLOGICAL = "METHODOLOGICAL"
    PHILOSOPHICAL = "PHILOSOPHICAL"
    TIMING = "TIMING"


class DebateMode(str, Enum):
    """Orchestrator phase: broad exploration vs. targeted convergence."""

    EXPLORE = "explore"
    EXPLOIT = "exploit"


class ConsensusType(str, Enum):
    """Degree of agreement reached by the committee."""

    CONSENSUS = "CONSENSUS"
    MAJORITY = "MAJORITY"
    CONTESTED = "CONTESTED"
    IRRECONCILABLE = "IRRECONCILABLE"


# ---------------------------------------------------------------------------
# Model 1 — AgentArgument
# ---------------------------------------------------------------------------


class AgentArgument(BaseModel):
    """Output produced by a single agent during one debate round."""

    model_config = ConfigDict(frozen=False)

    agent_id: str = Field(..., description="Stable machine identifier for the agent, e.g. 'warren'.")
    agent_name: str = Field(..., description="Human-readable display name, e.g. 'Warren · Value Investor'.")
    lens: str = Field(..., description="Analytical lens the agent applies, e.g. 'value', 'growth', 'macro'.")
    round_num: int = Field(..., ge=1, description="1-indexed debate round this argument belongs to.")
    verdict: Verdict = Field(..., description="Investment recommendation from this agent.")
    conviction_score: int = Field(..., ge=0, le=100, description="Agent's confidence in its verdict (0–100).")
    primary_argument: str = Field(..., description="2–3 sentence core investment thesis.")
    supporting_points: list[str] = Field(default_factory=list, description="Bullet evidence supporting the primary argument.")
    key_risks_to_view: list[str] = Field(default_factory=list, description="Risks the agent acknowledges to its own position.")
    what_would_change_my_mind: str = Field(..., description="Specific condition that would flip this verdict.")
    disagreement_with: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of {agent_name: point_of_contention} for agents this agent disputes.",
    )
    tokens_used: int = Field(default=0, ge=0, description="LLM tokens consumed generating this argument.")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="UTC timestamp when the argument was produced.")
    error: Optional[str] = Field(default=None, description="Non-null if the agent failed to produce a valid response.")

    # ------------------------------------------------------------------
    # Computed property
    # ------------------------------------------------------------------

    @computed_field  # type: ignore[misc]
    @property
    def verdict_color(self) -> str:
        """Return a Rich-compatible color string keyed to the agent's verdict.

        Usage:
            from rich.console import Console
            c = Console()
            c.print(f"[{arg.verdict_color}]{arg.verdict.value}[/{arg.verdict_color}]")
        """
        _MAP: dict[Verdict, str] = {
            Verdict.STRONG_BUY: "bold bright_green",
            Verdict.BUY: "green",
            Verdict.HOLD: "yellow",
            Verdict.SELL: "red",
            Verdict.STRONG_SELL: "bold bright_red",
        }
        return _MAP.get(self.verdict, "white")

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def parse_llm_response(cls, raw: str, **overrides: Any) -> "AgentArgument":
        """Parse a raw LLM response string into an AgentArgument.

        Handles:
        - Markdown code fences (```json ... ``` or ``` ... ```)
        - JSON embedded inside prose (extracts first {...} block)
        - Nested wrapper objects (unwraps one level if the required
          fields are not at the top level)
        - LLM field alias ``key_risks_to_your_view`` → ``key_risks_to_view``
        - Unknown fields are silently dropped (robust against prompt drift)

        Parameters
        ----------
        raw:
            The raw string returned by the LLM.
        **overrides:
            Merged after JSON parsing (agent_id, agent_name, round_num,
            lens, tokens_used — the caller-supplied context fields).

        Raises
        ------
        ValueError
            If no valid JSON containing the required fields can be extracted.
        """
        # 1. Strip markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip()).strip()

        # 2. Try to extract the first complete JSON object if embedded in prose
        if not cleaned.startswith("{"):
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(0)

        # 2.5. Resilient Repair: if JSON is truncated, try to close it
        def repair_json(s: str) -> str:
            stack = []
            for char in s:
                if char == '{': stack.append('}')
                elif char == '[': stack.append(']')
                elif char in ('}', ']'):
                    if stack and stack[-1] == char: stack.pop()
            return s + "".join(reversed(stack))

        try:
            payload: dict[str, Any] = json.loads(cleaned)
        except json.JSONDecodeError:
            # Try repair
            try:
                payload = json.loads(repair_json(cleaned))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"LLM response is not valid JSON even after repair.\n---\n{cleaned}\n---"
                ) from exc


        # 3. Recursive unwrap to find the required fields
        _REQUIRED = {"verdict", "primary_argument", "what_would_change_my_mind"}

        def recursive_find(data: Any) -> Optional[dict[str, Any]]:
            if isinstance(data, dict):
                if _REQUIRED.issubset(data.keys()):
                    return data
                for v in data.values():
                    res = recursive_find(v)
                    if res: return res
            elif isinstance(data, list):
                for item in data:
                    res = recursive_find(item)
                    if res: return res
            return None

        found = recursive_find(payload)
        if found:
            payload = found

        # 4. Normalise field aliases
        if "key_risks_to_your_view" in payload and "key_risks_to_view" not in payload:
            payload["key_risks_to_view"] = payload.pop("key_risks_to_your_view")


        # 5. Drop unknown fields (confidence_in_prior_round etc.)
        known_fields = set(cls.model_fields.keys())
        payload = {k: v for k, v in payload.items() if k in known_fields}

        # 6. Merge caller-supplied context (agent_id, round_num, etc.)
        payload.update(overrides)

        return cls.model_validate(payload)



# ---------------------------------------------------------------------------
# Model 2 — DisagreementRecord
# ---------------------------------------------------------------------------


class DisagreementRecord(BaseModel):
    """A tracked conflict between two agents, optionally resolved."""

    model_config = ConfigDict(frozen=False)

    agent_a: str = Field(..., description="agent_id of the first party in the conflict.")
    agent_b: str = Field(..., description="agent_id of the second party in the conflict.")
    conflict_type: ConflictType = Field(..., description="Nature of the disagreement.")
    crux: str = Field(..., description="The single assumption whose resolution would cause convergence.")
    extra_budget_allocated: int = Field(default=0, ge=0, description="Additional tokens allocated to resolve this conflict.")
    resolved: bool = Field(default=False, description="Whether the conflict has been resolved.")
    resolution: Optional[str] = Field(default=None, description="The ruling or resolution if resolved=True.")


# ---------------------------------------------------------------------------
# Model 3 — BudgetAllocation
# ---------------------------------------------------------------------------


class BudgetAllocation(BaseModel):
    """Per-agent, per-round token budget record."""

    model_config = ConfigDict(frozen=False)

    agent_id: str = Field(..., description="The agent this allocation belongs to.")
    round_num: int = Field(..., ge=1, description="The debate round this allocation covers.")
    tokens_allocated: int = Field(..., ge=0, description="Tokens budgeted before the round began.")
    tokens_used: int = Field(default=0, ge=0, description="Tokens actually consumed during the round.")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="UTC timestamp of the allocation record.")


# ---------------------------------------------------------------------------
# Model 4 — BudgetSummary
# ---------------------------------------------------------------------------


class BudgetSummary(BaseModel):
    """Rolled-up token budget view across all agents and rounds."""

    model_config = ConfigDict(frozen=False)

    total_budget: int = Field(..., ge=0, description="Total token budget for the entire debate.")
    total_spent: int = Field(default=0, ge=0, description="Total tokens consumed across all agents and rounds.")
    remaining: int = Field(default=0, description="Remaining tokens (total_budget - total_spent).")
    utilization_pct: float = Field(default=0.0, ge=0.0, le=100.0, description="Percentage of budget consumed (0–100).")
    allocations: list[BudgetAllocation] = Field(default_factory=list, description="Granular per-agent, per-round records.")
    extra_allocations_count: int = Field(default=0, ge=0, description="Number of extra token grants issued for contested points.")


# ---------------------------------------------------------------------------
# Model 5 — RoundSummary
# ---------------------------------------------------------------------------


class RoundSummary(BaseModel):
    """Aggregated view of one complete debate round."""

    model_config = ConfigDict(frozen=False)

    round_num: int = Field(..., ge=1, description="1-indexed debate round number.")
    mode: DebateMode = Field(..., description="Orchestrator phase active during this round.")
    arguments: list[AgentArgument] = Field(default_factory=list, description="Arguments produced by all agents this round.")
    disagreements: list[DisagreementRecord] = Field(default_factory=list, description="Conflicts detected or escalated this round.")
    convergence_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Normalised convergence metric (0 = total divergence, 1 = full consensus).")
    tokens_spent_this_round: int = Field(default=0, ge=0, description="Sum of tokens used by all agents this round.")


# ---------------------------------------------------------------------------
# Model 6 — AgentFinalPosition
# ---------------------------------------------------------------------------


class AgentFinalPosition(BaseModel):
    """An agent's definitive stance after all debate rounds have concluded."""

    model_config = ConfigDict(frozen=False)

    agent_id: str = Field(..., description="Stable machine identifier for the agent.")
    agent_name: str = Field(..., description="Human-readable display name.")
    lens: str = Field(..., description="Analytical lens of this agent.")
    final_verdict: Verdict = Field(..., description="The agent's verdict at the close of debate.")
    final_conviction_score: int = Field(..., ge=0, le=100, description="Conviction score at the close of debate.")
    position_changed: bool = Field(default=False, description="True if verdict or conviction shifted significantly during debate.")
    change_reason: Optional[str] = Field(default=None, description="What argument or evidence moved the agent's position.")


# ---------------------------------------------------------------------------
# Model 7 — CommitteeMemo
# ---------------------------------------------------------------------------


class CommitteeMemo(BaseModel):
    """The final synthesis memo produced after all debate rounds are complete."""

    model_config = ConfigDict(frozen=False)

    thesis: str = Field(..., description="The investment thesis that was debated.")
    verdict: Verdict = Field(..., description="Committee's final investment recommendation.")
    conviction_score: int = Field(..., ge=0, le=100, description="Committee-level conviction (not an average — reflects consensus quality).")
    consensus_type: ConsensusType = Field(..., description="Characterisation of how aligned the committee is.")
    bull_case: str = Field(..., description="Strongest version of the bullish argument from the debate.")
    bear_case: str = Field(..., description="Strongest version of the bearish argument from the debate.")
    key_risks: list[str] = Field(default_factory=list, description="Top risks regardless of final verdict.")
    key_catalysts: list[str] = Field(default_factory=list, description="Events or conditions that could accelerate the thesis.")
    agent_positions: list[AgentFinalPosition] = Field(default_factory=list, description="Each agent's final stance.")
    disagreements: list[DisagreementRecord] = Field(default_factory=list, description="All tracked conflicts, resolved or not.")
    unresolved_flags: list[str] = Field(default_factory=list, description="Issues flagged as unresolvable that must be disclosed.")
    recommended_action: str = Field(..., description="Specific actionable instruction (sizing, entry, horizon).")
    position_sizing: str = Field(..., description="Suggested allocation guidance, e.g. '3–5% of portfolio'.")
    conditions_to_revisit: list[str] = Field(default_factory=list, description="Explicit triggers that should prompt a re-evaluation.")
    budget_summary: BudgetSummary = Field(..., description="Full token budget accounting for this debate.")
    reasoning_trace: list[RoundSummary] = Field(default_factory=list, description="Ordered round-by-round debate history.")


# ---------------------------------------------------------------------------
# Model 8 — DebateTrace
# ---------------------------------------------------------------------------


class DebateTrace(BaseModel):
    """The full persisted record of a completed debate, including all rounds and synthesis."""

    model_config = ConfigDict(frozen=False)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, description="Unique identifier for this debate run.")
    thesis: str = Field(..., description="The investment thesis that was debated.")
    config: dict[str, Any] = Field(default_factory=dict, description="Orchestrator configuration snapshot (rounds, budget, agents, etc.).")
    rounds: list[RoundSummary] = Field(default_factory=list, description="All round summaries in chronological order.")
    synthesis: CommitteeMemo = Field(..., description="Final committee memo produced by the Synthesizer.")
    budget_summary: BudgetSummary = Field(..., description="Rolled-up budget across the entire debate.")
    mode_transitions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Log of explore→exploit transitions, each entry records the round and reason.",
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, description="UTC timestamp when this trace was persisted.")
