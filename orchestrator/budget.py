"""
orchestrator/budget.py
======================
Design spec (kept as documentation) + convenience re-export.

Implementation lives in orchestrator/budget_manager.py.

BudgetManager API
-----------------
__init__(total_budget, min_round_budget=1000, tiebreaker_reserve=2000)

allocate(agent_id, round_num, tokens) -> bool
    Reserve tokens for one agent in one round.
    Returns False if the grant would exceed total_budget.
    Raises BudgetExhaustedError when is_exhausted is True.
    Thread-safe via asyncio.Lock.

allocate_extra(agent_id, round_num, tokens, reason) -> bool
    Extra grant for conflict-resolution.
    Draws from main budget; logged separately in audit trail.
    Returns False (not raise) when budget is insufficient.

record_actual_usage(agent_id, round_num, actual_tokens) -> None
    Called after LLM response to record actual vs allocated.

can_afford_round(num_agents, tokens_per_agent) -> bool
can_afford_tiebreaker() -> bool

summary() -> BudgetSummary

Properties
----------
remaining      -> int
utilization    -> float   (0.0 – 1.0, based on allocated)
is_critical    -> bool    (>85 % allocated)
is_exhausted   -> bool    (>95 % allocated)
"""

from .budget_manager import BudgetExhaustedError, BudgetManager

__all__ = ["BudgetManager", "BudgetExhaustedError"]
