"""
orchestrator/budget_manager.py
===============================
Token budget accounting for the Investment Committee debate system.

Public surface
--------------
BudgetExhaustedError  – raised when allocate() is called on an exhausted budget
BudgetManager         – manages allocation, actual-usage recording, and audit trail
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from models.schemas import BudgetAllocation, BudgetSummary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_CRITICAL_PCT: float = 0.85   # >85 % spent → is_critical
_EXHAUSTED_PCT: float = 0.95  # >95 % spent → is_exhausted


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class BudgetExhaustedError(Exception):
    """Raised when :meth:`BudgetManager.allocate` is called on an exhausted budget.

    Attributes
    ----------
    remaining:
        Tokens still theoretically available at the time of the failed call.
    requested:
        Tokens that were requested and could not be granted.
    """

    def __init__(self, remaining: int, requested: int) -> None:
        super().__init__(
            f"Budget exhausted: requested {requested} tokens but only "
            f"{remaining} remain (>{_EXHAUSTED_PCT:.0%} spent)."
        )
        self.remaining = remaining
        self.requested = requested


# ---------------------------------------------------------------------------
# Internal allocation record (richer than the Pydantic schema — kept private)
# ---------------------------------------------------------------------------


@dataclass
class _AllocationEntry:
    """Internal record stored in the audit trail."""

    agent_id: str
    round_num: int
    tokens_allocated: int
    tokens_used: int = 0
    is_extra: bool = False          # True when created by allocate_extra()
    extra_reason: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# BudgetManager
# ---------------------------------------------------------------------------


class BudgetManager:
    """Manages the fixed token budget for the entire debate.

    Thread-safe for concurrent async agent calls via :class:`asyncio.Lock`.

    Parameters
    ----------
    total_budget:
        Hard ceiling on tokens for the whole debate (agents + synthesizer).
    min_round_budget:
        Minimum tokens required to start a new round.  Used by
        :meth:`can_afford_round` as a floor check.
    tiebreaker_reserve:
        Tokens reserved for Solomon tie-breaker calls.  :meth:`can_afford_tiebreaker`
        checks whether the reserve can still be honoured.

    Notes
    -----
    * ``allocated`` tracks committed tokens (sum of all :meth:`allocate` /
      :meth:`allocate_extra` grants).
    * ``spent`` tracks *actual* tokens (updated by :meth:`record_actual_usage`).
    * Properties such as :attr:`remaining` and :attr:`utilization` are based on
      **allocated** so the budget is protected even before LLM responses arrive.
    """

    def __init__(
        self,
        total_budget: int,
        min_round_budget: int = 1000,
        tiebreaker_reserve: int = 2000,
    ) -> None:
        if total_budget <= 0:
            raise ValueError(f"total_budget must be positive, got {total_budget}.")
        if tiebreaker_reserve >= total_budget:
            raise ValueError(
                f"tiebreaker_reserve ({tiebreaker_reserve}) must be less than "
                f"total_budget ({total_budget})."
            )

        self._total_budget: int = total_budget
        self._min_round_budget: int = min_round_budget
        self._tiebreaker_reserve: int = tiebreaker_reserve

        # Running counters — protected by _lock
        self._allocated: int = 0   # sum of granted allocations
        self._spent: int = 0       # sum of recorded actual usage
        self._extra_count: int = 0 # number of allocate_extra() grants

        # Full audit trail — every allocation/extra is appended here
        self._audit: List[_AllocationEntry] = []

        # Index: (agent_id, round_num) → _AllocationEntry for fast record_actual_usage
        # Only the *first* (non-extra) allocation per (agent, round) is indexed.
        self._index: dict[tuple[str, int], _AllocationEntry] = {}

        # Async lock for all mutating operations
        self._lock: asyncio.Lock = asyncio.Lock()

        logger.info(
            "[budget] Initialised — total=%d reserve=%d min_round=%d",
            total_budget,
            tiebreaker_reserve,
            min_round_budget,
        )

    # ------------------------------------------------------------------
    # Properties (read-only, lock-free — reads are atomic on CPython)
    # ------------------------------------------------------------------

    @property
    def remaining(self) -> int:
        """Uncommitted tokens = total_budget − allocated."""
        return self._total_budget - self._allocated

    @property
    def utilization(self) -> float:
        """Fraction of budget *allocated* (0.0–1.0)."""
        return self._allocated / self._total_budget

    @property
    def is_critical(self) -> bool:
        """True when more than 85 % of the budget has been allocated."""
        return self.utilization > _CRITICAL_PCT

    @property
    def is_exhausted(self) -> bool:
        """True when more than 95 % of the budget has been allocated."""
        return self.utilization > _EXHAUSTED_PCT

    # ------------------------------------------------------------------
    # Core allocation methods
    # ------------------------------------------------------------------

    async def allocate(
        self,
        agent_id: str,
        round_num: int,
        tokens: int,
    ) -> bool:
        """Reserve *tokens* for *agent_id* in *round_num*.

        Parameters
        ----------
        agent_id:
            Stable agent identifier (e.g. ``"warren"``).
        round_num:
            1-indexed debate round.
        tokens:
            Number of tokens to reserve.

        Returns
        -------
        bool
            ``True`` if the reservation succeeded; ``False`` if the grant
            would exceed the total budget (soft failure — caller decides
            whether to skip the agent or reduce the request).

        Raises
        ------
        BudgetExhaustedError
            If :attr:`is_exhausted` is ``True`` at the time of the call,
            meaning the budget is effectively depleted and no allocation
            should proceed.
        ValueError
            If *tokens* is not positive.
        """
        if tokens <= 0:
            raise ValueError(f"tokens must be positive, got {tokens}.")

        async with self._lock:
            if self.is_exhausted:
                raise BudgetExhaustedError(
                    remaining=self.remaining,
                    requested=tokens,
                )

            if self._allocated + tokens > self._total_budget:
                logger.warning(
                    "[budget] allocate DENIED — agent=%s round=%d requested=%d remaining=%d",
                    agent_id,
                    round_num,
                    tokens,
                    self.remaining,
                )
                return False

            self._allocated += tokens
            entry = _AllocationEntry(
                agent_id=agent_id,
                round_num=round_num,
                tokens_allocated=tokens,
            )
            self._audit.append(entry)

            # Index only the first normal allocation per (agent, round)
            key = (agent_id, round_num)
            if key not in self._index:
                self._index[key] = entry

            logger.debug(
                "[budget] allocate OK — agent=%s round=%d tokens=%d remaining=%d",
                agent_id,
                round_num,
                tokens,
                self.remaining,
            )
            return True

    async def allocate_extra(
        self,
        agent_id: str,
        round_num: int,
        tokens: int,
        reason: str,
    ) -> bool:
        """Grant extra tokens for conflict-resolution purposes.

        Draws from the same main budget but is flagged separately in the
        audit trail and counted in :attr:`BudgetSummary.extra_allocations_count`.

        Returns ``False`` (instead of raising) when the budget cannot cover
        the extra grant, so callers can degrade gracefully.
        """
        if tokens <= 0:
            raise ValueError(f"tokens must be positive, got {tokens}.")

        async with self._lock:
            if self._allocated + tokens > self._total_budget:
                logger.warning(
                    "[budget] allocate_extra DENIED — agent=%s round=%d reason=%r requested=%d remaining=%d",
                    agent_id,
                    round_num,
                    reason,
                    tokens,
                    self.remaining,
                )
                return False

            self._allocated += tokens
            self._extra_count += 1
            entry = _AllocationEntry(
                agent_id=agent_id,
                round_num=round_num,
                tokens_allocated=tokens,
                is_extra=True,
                extra_reason=reason,
            )
            self._audit.append(entry)

            logger.info(
                "[budget] allocate_extra OK — agent=%s round=%d tokens=%d reason=%r remaining=%d",
                agent_id,
                round_num,
                tokens,
                reason,
                self.remaining,
            )
            return True

    async def record_actual_usage(
        self,
        agent_id: str,
        round_num: int,
        actual_tokens: int,
    ) -> None:
        """Record actual tokens consumed after an LLM response arrives.

        Updates both the indexed entry (for per-agent audit) and the global
        ``_spent`` counter.  Over-usage beyond the allocated amount is logged
        as a warning but is **not** rejected — the LLM has already responded.

        Parameters
        ----------
        agent_id, round_num:
            Must match a prior :meth:`allocate` call for this pair.
        actual_tokens:
            Tokens reported by the LLM response's usage metadata.
        """
        if actual_tokens < 0:
            raise ValueError(f"actual_tokens cannot be negative, got {actual_tokens}.")

        async with self._lock:
            self._spent += actual_tokens
            key = (agent_id, round_num)
            entry = self._index.get(key)
            if entry is not None:
                entry.tokens_used = actual_tokens
                if actual_tokens > entry.tokens_allocated:
                    logger.warning(
                        "[budget] over-usage — agent=%s round=%d allocated=%d actual=%d delta=%d",
                        agent_id,
                        round_num,
                        entry.tokens_allocated,
                        actual_tokens,
                        actual_tokens - entry.tokens_allocated,
                    )
            else:
                logger.warning(
                    "[budget] record_actual_usage — no allocation found for agent=%s round=%d",
                    agent_id,
                    round_num,
                )

            logger.debug(
                "[budget] usage recorded — agent=%s round=%d actual=%d total_spent=%d",
                agent_id,
                round_num,
                actual_tokens,
                self._spent,
            )

    # ------------------------------------------------------------------
    # Affordability checks (synchronous — called before allocation)
    # ------------------------------------------------------------------

    def can_afford_round(self, num_agents: int, tokens_per_agent: int) -> bool:
        """Return True if the budget can cover one full round for all agents.

        Also ensures the result leaves at least :attr:`min_round_budget`
        unconsumed so subsequent rounds aren't completely starved.
        """
        needed = num_agents * tokens_per_agent
        after = self.remaining - needed
        return after >= self._min_round_budget

    def can_afford_tiebreaker(self) -> bool:
        """Return True if the tiebreaker reserve can still be honoured."""
        return self.remaining >= self._tiebreaker_reserve

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> BudgetSummary:
        """Return a :class:`~models.schemas.BudgetSummary` snapshot.

        The snapshot converts internal ``_AllocationEntry`` objects to
        Pydantic ``BudgetAllocation`` models.  Extra allocations are included
        in the flat list but also counted separately via
        ``extra_allocations_count``.
        """
        allocations = [
            BudgetAllocation(
                agent_id=e.agent_id,
                round_num=e.round_num,
                tokens_allocated=e.tokens_allocated,
                tokens_used=e.tokens_used,
                timestamp=e.timestamp,
            )
            for e in self._audit
        ]

        total_spent = self._spent
        utilization_pct = round((self._allocated / self._total_budget) * 100, 2)

        return BudgetSummary(
            total_budget=self._total_budget,
            total_spent=total_spent,
            remaining=self.remaining,
            utilization_pct=utilization_pct,
            allocations=allocations,
            extra_allocations_count=self._extra_count,
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"BudgetManager("
            f"total={self._total_budget}, "
            f"allocated={self._allocated}, "
            f"spent={self._spent}, "
            f"remaining={self.remaining}, "
            f"utilization={self.utilization:.1%})"
        )
