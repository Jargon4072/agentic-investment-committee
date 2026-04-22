"""
tests/test_synthesizer.py
==========================
Tests for Synthesizer — no LLM calls.
All verdict-rule logic is tested via _apply_verdict_rules() directly.
"""
import pytest
from models.schemas import (
    AgentArgument, AgentFinalPosition, BudgetAllocation, BudgetSummary,
    CommitteeMemo, ConsensusType, DisagreementRecord, RoundSummary,
    DebateMode, Verdict,
)
from synthesis.synthesizer import Synthesizer
from providers.base import LLMProvider, LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockProvider(LLMProvider):
    provider_name = "mock"
    def __init__(self, text=""):
        self._text = text
    async def complete(self, prompt, max_tokens, system=None, temperature=0.7):
        return LLMResponse(text=self._text, tokens_used=100, model="mock", latency_ms=5.0)
    async def stream(self, prompt, max_tokens, system=None):
        raise NotImplementedError


def make_arg(agent_id, verdict, conviction, tokens=500, lens="value", error=None):
    return AgentArgument(
        agent_id=agent_id,
        agent_name=agent_id.capitalize(),
        lens=lens,
        round_num=1,
        verdict=verdict,
        conviction_score=conviction,
        primary_argument=f"{agent_id} says: {verdict.value}",
        what_would_change_my_mind="Nothing.",
        tokens_used=tokens,
        error=error,
    )


def make_round(args, round_num=1):
    return RoundSummary(
        round_num=round_num,
        mode=DebateMode.EXPLORE,
        arguments=args,
    )


def make_budget(total=50000, spent=10000):
    return BudgetSummary(
        total_budget=total,
        total_spent=spent,
        remaining=total - spent,
        utilization_pct=round(spent / total * 100, 2),
    )


def base_memo(thesis="NVDA?", verdict=Verdict.HOLD, consensus=ConsensusType.CONTESTED):
    return CommitteeMemo(
        thesis=thesis,
        verdict=verdict,
        conviction_score=60,
        consensus_type=consensus,
        bull_case="Bull.",
        bear_case="Bear.",
        recommended_action="Review.",
        position_sizing="3-5%",
        budget_summary=make_budget(),
    )


S = Synthesizer(MockProvider())


# ---------------------------------------------------------------------------
# Rule 1: IRRECONCILABLE -> HOLD
# ---------------------------------------------------------------------------

def test_irreconcilable_sets_hold():
    memo = base_memo(consensus=ConsensusType.IRRECONCILABLE)
    args = [make_arg("warren", Verdict.STRONG_BUY, 90)]
    rounds = [make_round(args)]
    S._apply_verdict_rules(memo, rounds)
    assert memo.verdict == Verdict.HOLD
    assert memo.consensus_type == ConsensusType.IRRECONCILABLE


# ---------------------------------------------------------------------------
# Rule 2: 3+ agents agree -> majority
# ---------------------------------------------------------------------------

def test_majority_3_of_4():
    args = [
        make_arg("warren", Verdict.BUY, 80, tokens=600),
        make_arg("cathie", Verdict.BUY, 75, tokens=500),
        make_arg("ray",    Verdict.BUY, 70, tokens=400),
        make_arg("nassim", Verdict.SELL, 85, tokens=550),
    ]
    memo = base_memo()
    S._apply_verdict_rules(memo, [make_round(args)])
    assert memo.verdict == Verdict.BUY
    assert memo.consensus_type == ConsensusType.MAJORITY


def test_consensus_all_4_agree():
    args = [make_arg(f"a{i}", Verdict.STRONG_BUY, 90) for i in range(4)]
    memo = base_memo()
    S._apply_verdict_rules(memo, [make_round(args)])
    assert memo.verdict == Verdict.STRONG_BUY
    assert memo.consensus_type == ConsensusType.CONSENSUS


# ---------------------------------------------------------------------------
# Rule 3: 2-2 split -> CONTESTED, tiebreaker decides
# ---------------------------------------------------------------------------

def test_split_no_solomon_stays_contested():
    args = [
        make_arg("warren", Verdict.BUY, 80),
        make_arg("cathie", Verdict.BUY, 75),
        make_arg("ray",    Verdict.SELL, 70),
        make_arg("nassim", Verdict.SELL, 85),
    ]
    memo = base_memo(verdict=Verdict.HOLD)
    S._apply_verdict_rules(memo, [make_round(args)])
    assert memo.consensus_type == ConsensusType.CONTESTED


def test_split_with_solomon_uses_tiebreaker():
    args = [
        make_arg("warren", Verdict.BUY, 80),
        make_arg("cathie", Verdict.BUY, 75),
        make_arg("ray",    Verdict.SELL, 70),
        make_arg("nassim", Verdict.SELL, 85),
        make_arg("solomon", Verdict.STRONG_BUY, 70),   # tiebreaker
    ]
    memo = base_memo(verdict=Verdict.HOLD)
    S._apply_verdict_rules(memo, [make_round(args)])
    assert memo.consensus_type == ConsensusType.CONTESTED
    assert memo.verdict == Verdict.STRONG_BUY   # Solomon's verdict


# ---------------------------------------------------------------------------
# Rule 4: conviction_score = tokens-weighted average
# ---------------------------------------------------------------------------

def test_conviction_tokens_weighted():
    # warren: 80 conviction, 1000 tokens; cathie: 60 conviction, 500 tokens
    # 3 agents vote BUY → majority rule fires, then conviction weighted
    args = [
        make_arg("warren", Verdict.BUY, 80, tokens=1000),
        make_arg("cathie", Verdict.BUY, 60, tokens=500),
        make_arg("ray",    Verdict.BUY, 70, tokens=500),
        make_arg("nassim", Verdict.HOLD, 50, tokens=500),
    ]
    memo = base_memo()
    S._apply_verdict_rules(memo, [make_round(args)])
    # Weighted avg: (80×1000 + 60×500 + 70×500 + 50×500) / 2500
    # = (80000 + 30000 + 35000 + 25000) / 2500 = 170000/2500 = 68
    assert memo.conviction_score == 68


def test_conviction_equal_tokens_is_simple_average():
    args = [make_arg(f"a{i}", Verdict.BUY, 70 + i * 10, tokens=500) for i in range(3)]
    # verdicts: BUY/BUY/BUY, convictions: 70, 80, 90 → avg = 80
    memo = base_memo()
    S._apply_verdict_rules(memo, [make_round(args)])
    assert memo.conviction_score == 80


# ---------------------------------------------------------------------------
# Error sentinel exclusion
# ---------------------------------------------------------------------------

def test_error_sentinels_excluded_from_verdict():
    args = [
        make_arg("warren", Verdict.BUY, 80),
        make_arg("cathie", Verdict.BUY, 75),
        make_arg("ray",    Verdict.BUY, 70),
        make_arg("nassim", Verdict.STRONG_SELL, 90, error="parse failed"),  # excluded
    ]
    memo = base_memo()
    S._apply_verdict_rules(memo, [make_round(args)])
    # 3 valid BUY votes → CONSENSUS
    assert memo.verdict == Verdict.BUY
    assert memo.consensus_type == ConsensusType.CONSENSUS


# ---------------------------------------------------------------------------
# _fallback_memo
# ---------------------------------------------------------------------------

def test_fallback_never_raises():
    rounds = [make_round([
        make_arg("warren", Verdict.BUY, 80),
        make_arg("cathie", Verdict.SELL, 70),
    ])]
    memo = S._fallback_memo("test thesis", rounds, make_budget())
    assert isinstance(memo, CommitteeMemo)
    assert memo.thesis == "test thesis"


def test_fallback_extracts_bull_and_bear():
    rounds = [make_round([
        make_arg("warren", Verdict.STRONG_BUY, 90),
        make_arg("nassim", Verdict.STRONG_SELL, 85),
    ])]
    memo = S._fallback_memo("NVDA?", rounds, make_budget())
    assert "warren" in memo.bull_case.lower() or "STRONG BUY" in memo.bull_case
    assert "nassim" in memo.bear_case.lower() or "STRONG SELL" in memo.bear_case


# ---------------------------------------------------------------------------
# Import / syntax check
# ---------------------------------------------------------------------------

def test_synthesizer_import():
    from synthesis.synthesizer import Synthesizer
    s = Synthesizer(MockProvider())
    assert s.model is not None
