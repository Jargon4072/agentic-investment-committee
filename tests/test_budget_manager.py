"""
tests/test_budget_manager.py
=============================
Comprehensive tests for BudgetManager.
Covers: allocation, extra allocation, actual usage recording,
affordability checks, BudgetExhaustedError, audit trail, and summary.
"""

import asyncio
import pytest

from orchestrator.budget_manager import BudgetExhaustedError, BudgetManager
from models.schemas import BudgetSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Run an async coroutine synchronously (works on Python 3.9)."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# __init__ validation
# ---------------------------------------------------------------------------

def test_init_rejects_zero_budget():
    with pytest.raises(ValueError):
        BudgetManager(total_budget=0)


def test_init_rejects_reserve_gte_budget():
    with pytest.raises(ValueError):
        BudgetManager(total_budget=5000, tiebreaker_reserve=5000)


def test_repr_contains_key_info():
    bm = BudgetManager(total_budget=10_000)
    r = repr(bm)
    assert "10000" in r
    assert "remaining" in r


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def test_initial_state():
    bm = BudgetManager(total_budget=10_000)
    assert bm.remaining == 10_000
    assert bm.utilization == 0.0
    assert not bm.is_critical
    assert not bm.is_exhausted


def test_remaining_decreases_on_allocate():
    bm = BudgetManager(total_budget=10_000)
    run(bm.allocate("warren", 1, 2000))
    assert bm.remaining == 8_000


def test_utilization_after_half_allocated():
    bm = BudgetManager(total_budget=10_000)
    run(bm.allocate("warren", 1, 5000))
    assert bm.utilization == pytest.approx(0.5)


def test_is_critical_threshold():
    bm = BudgetManager(total_budget=10_000, min_round_budget=0)
    run(bm.allocate("warren", 1, 8_600))   # 86 %
    assert bm.is_critical
    assert not bm.is_exhausted


def test_is_exhausted_threshold():
    bm = BudgetManager(total_budget=10_000, min_round_budget=0)
    run(bm.allocate("warren", 1, 9_600))   # 96 %
    assert bm.is_exhausted
    assert bm.is_critical


# ---------------------------------------------------------------------------
# allocate()
# ---------------------------------------------------------------------------

def test_allocate_returns_true_on_success():
    bm = BudgetManager(total_budget=10_000)
    result = run(bm.allocate("warren", 1, 1000))
    assert result is True


def test_allocate_returns_false_when_would_exceed():
    bm = BudgetManager(total_budget=5_000)
    run(bm.allocate("warren", 1, 4_000))
    # 4001 would exceed 5000
    result = run(bm.allocate("cathie", 1, 1_001))
    assert result is False
    # Remaining should still be 1000 — denied grant not committed
    assert bm.remaining == 1_000


def test_allocate_raises_when_exhausted():
    bm = BudgetManager(total_budget=10_000, min_round_budget=0)
    run(bm.allocate("warren", 1, 9_600))   # push past 95 %
    assert bm.is_exhausted
    with pytest.raises(BudgetExhaustedError) as exc_info:
        run(bm.allocate("cathie", 1, 100))
    assert exc_info.value.requested == 100


def test_allocate_rejects_non_positive_tokens():
    bm = BudgetManager(total_budget=10_000)
    with pytest.raises(ValueError):
        run(bm.allocate("warren", 1, 0))


# ---------------------------------------------------------------------------
# allocate_extra()
# ---------------------------------------------------------------------------

def test_allocate_extra_returns_true():
    bm = BudgetManager(total_budget=10_000)
    result = run(bm.allocate_extra("solomon", 2, 500, reason="tie-break"))
    assert result is True
    assert bm.remaining == 9_500


def test_allocate_extra_returns_false_not_raise_when_insufficient():
    bm = BudgetManager(total_budget=5_000)
    run(bm.allocate("warren", 1, 4_900))
    # Only 100 left — request 200
    result = run(bm.allocate_extra("solomon", 1, 200, reason="contest"))
    assert result is False
    # Remaining unchanged
    assert bm.remaining == 100


def test_allocate_extra_increments_extra_count():
    bm = BudgetManager(total_budget=10_000)
    run(bm.allocate_extra("solomon", 1, 300, reason="r1"))
    run(bm.allocate_extra("solomon", 2, 300, reason="r2"))
    s = bm.summary()
    assert s.extra_allocations_count == 2


# ---------------------------------------------------------------------------
# record_actual_usage()
# ---------------------------------------------------------------------------

def test_record_actual_usage_updates_spent():
    bm = BudgetManager(total_budget=10_000)
    run(bm.allocate("warren", 1, 1000))
    run(bm.record_actual_usage("warren", 1, 850))
    s = bm.summary()
    assert s.total_spent == 850


def test_record_actual_usage_no_error_for_unknown_key():
    # Should not raise even if no prior allocation found
    bm = BudgetManager(total_budget=10_000)
    run(bm.record_actual_usage("ghost", 99, 100))   # no exception


def test_record_actual_usage_updates_allocation_entry():
    bm = BudgetManager(total_budget=10_000)
    run(bm.allocate("warren", 1, 1000))
    run(bm.record_actual_usage("warren", 1, 750))
    s = bm.summary()
    entry = next(a for a in s.allocations if a.agent_id == "warren" and a.round_num == 1)
    assert entry.tokens_used == 750


def test_record_actual_usage_rejects_negative():
    bm = BudgetManager(total_budget=10_000)
    with pytest.raises(ValueError):
        run(bm.record_actual_usage("warren", 1, -1))


# ---------------------------------------------------------------------------
# can_afford_round() / can_afford_tiebreaker()
# ---------------------------------------------------------------------------

def test_can_afford_round_true_when_sufficient():
    bm = BudgetManager(total_budget=20_000, min_round_budget=1000)
    assert bm.can_afford_round(num_agents=4, tokens_per_agent=2000)


def test_can_afford_round_false_when_would_starve_next_round():
    # 4 agents × 2000 = 8000, remaining = 8500, leaves only 500 < min 1000
    bm = BudgetManager(total_budget=20_000, min_round_budget=1000)
    run(bm.allocate("w", 1, 11_500))   # consumed 11_500, remaining = 8_500
    assert not bm.can_afford_round(num_agents=4, tokens_per_agent=2000)


def test_can_afford_tiebreaker_true():
    bm = BudgetManager(total_budget=10_000, tiebreaker_reserve=2000)
    assert bm.can_afford_tiebreaker()


def test_can_afford_tiebreaker_false_when_reserve_eaten():
    bm = BudgetManager(total_budget=10_000, tiebreaker_reserve=2000, min_round_budget=0)
    run(bm.allocate("w", 1, 9_000))   # remaining = 1000 < reserve 2000
    assert not bm.can_afford_tiebreaker()


# ---------------------------------------------------------------------------
# summary()
# ---------------------------------------------------------------------------

def test_summary_returns_budget_summary_type():
    bm = BudgetManager(total_budget=10_000)
    assert isinstance(bm.summary(), BudgetSummary)


def test_summary_full_audit_trail():
    bm = BudgetManager(total_budget=20_000)
    run(bm.allocate("warren", 1, 1000))
    run(bm.allocate("cathie", 1, 1000))
    run(bm.allocate_extra("solomon", 1, 500, reason="contest"))
    run(bm.record_actual_usage("warren", 1, 900))
    run(bm.record_actual_usage("cathie", 1, 1050))

    s = bm.summary()
    assert s.total_budget == 20_000
    assert s.total_spent == 900 + 1050
    assert s.remaining == 20_000 - 2_500   # 17_500
    assert s.extra_allocations_count == 1
    assert len(s.allocations) == 3         # 2 normal + 1 extra


def test_summary_utilization_pct_range():
    bm = BudgetManager(total_budget=10_000, min_round_budget=0)
    run(bm.allocate("w", 1, 3000))
    s = bm.summary()
    assert 0.0 <= s.utilization_pct <= 100.0
    assert s.utilization_pct == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Concurrency smoke test
# ---------------------------------------------------------------------------

def test_concurrent_allocations_do_not_exceed_budget():
    """Fire 20 concurrent allocate() calls and verify total committed <= budget.

    With 20 tasks each requesting 1000 tokens against a 10_000 budget:
    - The first 10 successful grants fill the budget to 100 % (is_exhausted = True)
    - Subsequent callers either get False (budget exceeded before exhausted)
      or BudgetExhaustedError (budget already at >95 %)
    We use return_exceptions=True so gather does not abort on the first error.
    """
    async def _run():
        bm = BudgetManager(total_budget=10_000, min_round_budget=0)
        tasks = [bm.allocate(f"agent_{i}", 1, 1000) for i in range(20)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        granted = sum(1 for r in results if r is True)
        denied  = sum(1 for r in results if r is False)
        errored = sum(1 for r in results if isinstance(r, BudgetExhaustedError))

        # Hard invariant: never over-commit
        assert bm._allocated <= 10_000, f"Over-committed: {bm._allocated}"
        # Every result must be one of the three expected outcomes
        assert granted + denied + errored == 20
        # At most 10 grants possible (10 × 1000 = 10_000)
        assert granted <= 10
        return granted, denied, errored

    granted, denied, errored = asyncio.get_event_loop().run_until_complete(_run())
    # Sanity: something was granted and at least some were rejected/errored
    assert granted > 0
    assert denied + errored > 0

