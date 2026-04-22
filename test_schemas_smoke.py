from models.schemas import (
    Verdict, ConflictType, DebateMode, ConsensusType,
    AgentArgument, DisagreementRecord, BudgetAllocation, BudgetSummary,
    RoundSummary, AgentFinalPosition, CommitteeMemo, DebateTrace,
)

# ── smoke test parse_llm_response ──────────────────────────────────────────
raw = (
    "```json\n"
    '{\n'
    '    "verdict": "BUY",\n'
    '    "conviction_score": 72,\n'
    '    "primary_argument": "Strong FCF yield with a 30% margin of safety.",\n'
    '    "supporting_points": ["Point A", "Point B"],\n'
    '    "key_risks_to_your_view": ["Rising rates"],\n'
    '    "what_would_change_my_mind": "FCF yield drops below 5%.",\n'
    '    "disagreement_with": {"cathie": "TAM is speculative"}\n'
    "}\n"
    "```"
)

arg = AgentArgument.parse_llm_response(
    raw,
    agent_id="warren",
    agent_name="Warren · Value Investor",
    lens="value",
    round_num=1,
    tokens_used=512,
)

assert arg.verdict == Verdict.BUY, f"unexpected verdict {arg.verdict}"
assert arg.verdict_color == "green", f"unexpected color {arg.verdict_color}"
assert arg.key_risks_to_view == ["Rising rates"], f"risks alias failed: {arg.key_risks_to_view}"
assert arg.error is None

# ── check all enums have expected members ──────────────────────────────────
assert Verdict.STRONG_BUY.value == "STRONG BUY"
assert ConflictType.PHILOSOPHICAL.value == "PHILOSOPHICAL"
assert DebateMode.EXPLORE.value == "explore"
assert ConsensusType.IRRECONCILABLE.value == "IRRECONCILABLE"

print("verdict :", arg.verdict)
print("color   :", arg.verdict_color)
print("risks   :", arg.key_risks_to_view)
print()
print("ALL ASSERTIONS PASSED")
