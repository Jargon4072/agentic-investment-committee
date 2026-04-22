"""
tests/test_disagreement_detector.py
=====================================
Tests for DisagreementDetector.
No LLM calls — all arguments are hand-crafted.
"""

import pytest
from models.schemas import AgentArgument, ConflictType, DisagreementRecord, Verdict
from orchestrator.disagreement_detector import DisagreementDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_arg(
    agent_id: str,
    verdict: Verdict,
    conviction: int,
    lens: str = "value",
    primary: str = "Default primary argument text.",
    wcmm: str = "Nothing would change my mind.",
    disagreement_with: dict = None,
    error: str = None,
) -> AgentArgument:
    return AgentArgument(
        agent_id=agent_id,
        agent_name=agent_id.capitalize(),
        lens=lens,
        round_num=1,
        verdict=verdict,
        conviction_score=conviction,
        primary_argument=primary,
        what_would_change_my_mind=wcmm,
        disagreement_with=disagreement_with or {},
        error=error,
    )


D = DisagreementDetector()


# ---------------------------------------------------------------------------
# detect() — basic cases
# ---------------------------------------------------------------------------

def test_detect_empty_returns_empty():
    assert D.detect([]) == []


def test_detect_single_agent_returns_empty():
    assert D.detect([make_arg("warren", Verdict.BUY, 70)]) == []


def test_detect_agreement_returns_empty():
    args = [
        make_arg("warren", Verdict.BUY, 70),
        make_arg("cathie", Verdict.BUY, 75),
    ]
    assert D.detect(args) == []


def test_detect_error_sentinel_skipped():
    """An agent with error set must not generate any conflicts."""
    args = [
        make_arg("warren", Verdict.STRONG_BUY, 90),
        make_arg("cathie", Verdict.STRONG_SELL, 90, error="parse failed"),
    ]
    assert D.detect(args) == []


# ---------------------------------------------------------------------------
# Rule 1 — HARD CONFLICT
# ---------------------------------------------------------------------------

def test_hard_conflict_strong_buy_vs_sell():
    args = [
        make_arg("warren", Verdict.STRONG_BUY, 85),
        make_arg("nassim", Verdict.SELL, 80),
    ]
    records = D.detect(args)
    assert len(records) == 1
    assert {records[0].agent_a, records[0].agent_b} == {"nassim", "warren"}


def test_hard_conflict_strong_buy_vs_strong_sell():
    args = [
        make_arg("cathie", Verdict.STRONG_BUY, 90),
        make_arg("warren", Verdict.STRONG_SELL, 85),
    ]
    records = D.detect(args)
    assert len(records) == 1


def test_hard_conflict_buy_vs_strong_sell():
    args = [
        make_arg("ray", Verdict.BUY, 70),
        make_arg("nassim", Verdict.STRONG_SELL, 80),
    ]
    records = D.detect(args)
    assert len(records) == 1


def test_no_hard_conflict_buy_vs_sell():
    """BUY vs SELL gap = 2, below the threshold of 3."""
    args = [
        make_arg("warren", Verdict.BUY, 65),
        make_arg("nassim", Verdict.SELL, 70),
    ]
    records = D.detect(args)
    # No hard conflict — only score-spread or explicit can trigger
    assert all(r.conflict_type != ConflictType.PHILOSOPHICAL for r in records)
    # Score spread = 5, well below 35 — should be empty
    assert records == []


# ---------------------------------------------------------------------------
# Rule 2 — SCORE CONFLICT
# ---------------------------------------------------------------------------

def test_score_conflict_large_spread_different_verdicts():
    args = [
        make_arg("warren", Verdict.BUY, 80, lens="value"),
        make_arg("nassim", Verdict.HOLD, 40, lens="risk"),
    ]
    records = D.detect(args)
    assert len(records) == 1
    assert records[0].conflict_type == ConflictType.METHODOLOGICAL


def test_score_conflict_ignored_if_same_verdict():
    """Large score spread but same verdict → no conflict."""
    args = [
        make_arg("warren", Verdict.BUY, 90),
        make_arg("cathie", Verdict.BUY, 50),
    ]
    assert D.detect(args) == []


def test_score_conflict_exactly_at_threshold_ignored():
    """Spread of exactly 35 is NOT > 35, so no conflict."""
    args = [
        make_arg("warren", Verdict.BUY, 80),
        make_arg("nassim", Verdict.HOLD, 45),
    ]
    assert D.detect(args) == []


# ---------------------------------------------------------------------------
# Rule 3 — EXPLICIT DISAGREEMENT
# ---------------------------------------------------------------------------

def test_explicit_disagreement_by_agent_id():
    args = [
        make_arg("warren", Verdict.HOLD, 55, disagreement_with={"cathie": "TAM is speculative"}),
        make_arg("cathie", Verdict.BUY, 70),
    ]
    records = D.detect(args)
    assert len(records) == 1
    assert records[0].crux == "TAM is speculative"


def test_explicit_disagreement_by_agent_name():
    """disagreement_with can reference display name (agent_name)."""
    args = [
        make_arg("warren", Verdict.HOLD, 55, disagreement_with={"Cathie": "S-curve unproven"}),
        make_arg("cathie", Verdict.BUY, 70),
    ]
    records = D.detect(args)
    assert len(records) == 1


def test_explicit_disagreement_unknown_agent_ignored():
    """Reference to agent not in panel must not crash or produce a record."""
    args = [
        make_arg("warren", Verdict.HOLD, 55, disagreement_with={"gandalf": "No wizards in this panel"}),
        make_arg("cathie", Verdict.BUY, 70),
    ]
    assert D.detect(args) == []


# ---------------------------------------------------------------------------
# No duplicate pairs
# ---------------------------------------------------------------------------

def test_no_duplicate_pairs_when_both_rules_match():
    """HARD conflict + explicit disagreement for same pair = 1 record."""
    args = [
        make_arg("warren", Verdict.STRONG_BUY, 90, disagreement_with={"nassim": "Tail risk overblown"}),
        make_arg("nassim", Verdict.STRONG_SELL, 85),
    ]
    records = D.detect(args)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Conflict type classification
# ---------------------------------------------------------------------------

def test_methodological_for_different_lenses():
    args = [
        make_arg("warren", Verdict.STRONG_BUY, 90, lens="value"),
        make_arg("nassim", Verdict.SELL, 80, lens="risk"),
    ]
    records = D.detect(args)
    assert records[0].conflict_type == ConflictType.METHODOLOGICAL


def test_factual_for_same_lens():
    args = [
        make_arg("warren", Verdict.STRONG_BUY, 90, lens="value"),
        make_arg("otherguy", Verdict.SELL, 85, lens="value"),
    ]
    records = D.detect(args)
    assert records[0].conflict_type == ConflictType.FACTUAL


def test_philosophical_when_wcmm_phrase_in_other_primary():
    """If warren's flip-condition phrase appears in cathie's primary argument."""
    args = [
        make_arg(
            "warren", Verdict.STRONG_BUY, 90, lens="value",
            wcmm="if free cash flow yield drops below five percent i would exit",
        ),
        make_arg(
            "cathie", Verdict.STRONG_SELL, 80, lens="growth",
            primary="Free cash flow yield drops below five percent in every bear case.",
        ),
    ]
    records = D.detect(args)
    assert records[0].conflict_type == ConflictType.PHILOSOPHICAL


# ---------------------------------------------------------------------------
# has_irreconcilable()
# ---------------------------------------------------------------------------

def test_irreconcilable_two_philosophical_conflicts():
    records = [
        DisagreementRecord(agent_a="warren", agent_b="cathie",
                           conflict_type=ConflictType.PHILOSOPHICAL,
                           crux="crux1"),
        DisagreementRecord(agent_a="ray", agent_b="nassim",
                           conflict_type=ConflictType.PHILOSOPHICAL,
                           crux="crux2"),
    ]
    assert D.has_irreconcilable(records) is True


def test_not_irreconcilable_one_philosophical():
    records = [
        DisagreementRecord(agent_a="warren", agent_b="cathie",
                           conflict_type=ConflictType.PHILOSOPHICAL,
                           crux="crux1"),
        DisagreementRecord(agent_a="ray", agent_b="nassim",
                           conflict_type=ConflictType.METHODOLOGICAL,
                           crux="crux2"),
    ]
    assert D.has_irreconcilable(records) is False


def test_irreconcilable_all_distinct_verdicts():
    args = [
        make_arg("warren", Verdict.STRONG_BUY, 90),
        make_arg("cathie", Verdict.BUY, 70),
        make_arg("ray",    Verdict.SELL, 60),
        make_arg("nassim", Verdict.STRONG_SELL, 85),
    ]
    assert D.has_irreconcilable([], arguments=args) is True


def test_not_irreconcilable_three_distinct_verdicts():
    """Only 3 distinct verdicts out of 4 agents — does not trigger."""
    args = [
        make_arg("warren", Verdict.STRONG_BUY, 90),
        make_arg("cathie", Verdict.BUY, 70),
        make_arg("ray",    Verdict.SELL, 60),
        make_arg("nassim", Verdict.SELL, 75),   # same as ray
    ]
    assert D.has_irreconcilable([], arguments=args) is False


def test_irreconcilable_score_spread_over_60():
    args = [
        make_arg("warren", Verdict.BUY, 95),
        make_arg("cathie", Verdict.BUY, 34),
    ]
    assert D.has_irreconcilable([], arguments=args) is True


def test_not_irreconcilable_score_spread_exactly_60():
    args = [
        make_arg("warren", Verdict.BUY, 90),
        make_arg("cathie", Verdict.BUY, 30),
    ]
    assert D.has_irreconcilable([], arguments=args) is False


# ---------------------------------------------------------------------------
# get_most_contested_agents()
# ---------------------------------------------------------------------------

def test_most_contested_empty():
    assert D.get_most_contested_agents([]) is None


def test_most_contested_single_record():
    records = [
        DisagreementRecord(agent_a="warren", agent_b="cathie",
                           conflict_type=ConflictType.METHODOLOGICAL, crux="x"),
    ]
    pair = D.get_most_contested_agents(records)
    assert set(pair) == {"warren", "cathie"}


def test_most_contested_multi_record():
    records = [
        DisagreementRecord(agent_a="warren", agent_b="cathie",
                           conflict_type=ConflictType.METHODOLOGICAL, crux="x"),
        DisagreementRecord(agent_a="cathie", agent_b="warren",
                           conflict_type=ConflictType.PHILOSOPHICAL, crux="y"),
        DisagreementRecord(agent_a="ray", agent_b="nassim",
                           conflict_type=ConflictType.FACTUAL, crux="z"),
    ]
    pair = D.get_most_contested_agents(records)
    # warren/cathie appear in 2 records vs ray/nassim in 1
    assert set(pair) == {"warren", "cathie"}
