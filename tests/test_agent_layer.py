"""
Structural test for the agent layer.
No LLM calls — uses a MockProvider.
"""

import asyncio
import os
import sys

# ── make sure project root is on path ────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents import (
    AgentConfig, BaseAgent,
    ValueAgent, GrowthAgent, MacroAgent, RiskAgent, TiebreakerAgent,
    build_panel, build_tiebreaker, build_all,
)
from models.schemas import AgentArgument, Verdict
from providers.base import LLMProvider, LLMResponse, ProviderError

# ── Mock provider ─────────────────────────────────────────────────────────────

VALID_JSON = """{
    "verdict": "BUY",
    "conviction_score": 72,
    "primary_argument": "Strong FCF yield with 30% margin of safety.",
    "supporting_points": ["Point A", "Point B"],
    "key_risks_to_your_view": ["Rising rates"],
    "what_would_change_my_mind": "FCF yield drops below 5%.",
    "disagreement_with": {"Cathie": "TAM assumptions are speculative"},
    "confidence_in_prior_round": null
}"""

class MockProvider(LLMProvider):
    provider_name = "mock"

    def __init__(self, response_text: str = VALID_JSON, fail: bool = False):
        self._text = response_text
        self._fail = fail

    async def complete(self, prompt, max_tokens, system=None, temperature=0.7):
        if self._fail:
            raise ProviderError("mock failure", provider="mock")
        return LLMResponse(text=self._text, tokens_used=300, model="mock", latency_ms=10.0)

    async def stream(self, prompt, max_tokens, system=None):
        raise NotImplementedError

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

# ── 1. AgentConfig is frozen ──────────────────────────────────────────────────
cfg = AgentConfig(id="x", name="X", lens="test", system_prompt="s", display_name="X·Test")
try:
    cfg.id = "y"
    assert False, "Should be frozen"
except Exception:
    pass
print("AgentConfig frozen OK")

# ── 2. Concrete agents have correct ids / lenses ──────────────────────────────
p = MockProvider()
checks = [
    (ValueAgent(p),      "warren", "value",     "Warren · Value Investor"),
    (GrowthAgent(p),     "cathie", "growth",    "Cathie · Growth Optimist"),
    (MacroAgent(p),      "ray",    "macro",     "Ray · Macro Strategist"),
    (RiskAgent(p),       "nassim", "risk",      "Nassim · Risk Manager"),
    (TiebreakerAgent(p), "solomon","committee", "Solomon · Committee Chair"),
]
for agent, exp_id, exp_lens, exp_display in checks:
    assert agent.config.id == exp_id,           f"{exp_id}: id mismatch"
    assert agent.config.lens == exp_lens,        f"{exp_id}: lens mismatch"
    assert agent.config.display_name == exp_display, f"{exp_id}: display_name mismatch"
    assert isinstance(agent, BaseAgent),         f"{exp_id}: not BaseAgent"
print("All agent identities OK")

# ── 3. factory functions return correct shapes ────────────────────────────────
panel = build_panel(p)
assert len(panel) == 4
assert [a.config.id for a in panel] == ["warren", "cathie", "ray", "nassim"]

tb = build_tiebreaker(p)
assert tb.config.id == "solomon"

all_agents = build_all(p)
assert set(all_agents.keys()) == {"warren", "cathie", "ray", "nassim", "solomon"}
print("Factory functions OK")

# ── 4. _build_prompt for round 1 (no prior args) ─────────────────────────────
agent = ValueAgent(p)
prompt = agent._build_prompt("Is NVDA fairly valued?", round_num=1, prior_arguments=[])
assert "INVESTMENT THESIS: Is NVDA fairly valued?" in prompt
assert "Round 1" in prompt
assert "opening round" in prompt
assert "Prior Committee" not in prompt
print("_build_prompt round 1 OK")

# ── 5. _build_prompt for round 2 with prior args ─────────────────────────────
prior = AgentArgument(
    agent_id="cathie", agent_name="Cathie · Growth Optimist", lens="growth",
    round_num=1, verdict=Verdict.STRONG_BUY, conviction_score=85,
    primary_argument="S-curve inflection is underpriced.",
    what_would_change_my_mind="Revenue growth drops below 20%.",
    disagreement_with={"warren": "DCF is the wrong model for this asset"},
)
prompt2 = agent._build_prompt("Is NVDA fairly valued?", round_num=2, prior_arguments=[prior])
assert "[Round 1] Cathie · Growth Optimist (growth): STRONG BUY (85/100)" in prompt2
assert "S-curve inflection is underpriced." in prompt2
assert "DCF is the wrong model for this asset" in prompt2   # concern directed at warren
assert "update your position" in prompt2.lower()
print("_build_prompt round 2 with prior args OK")

# ── 6. argue() — happy path ───────────────────────────────────────────────────
result = run(agent.argue("Is NVDA fairly valued?", round_num=1, prior_arguments=[], max_tokens=512))
assert isinstance(result, AgentArgument)
assert result.error is None
assert result.verdict == Verdict.BUY
assert result.agent_id == "warren"
assert result.tokens_used == 300
print("argue() happy path OK")

# ── 7. argue() — provider failure returns error sentinel ─────────────────────
fail_agent = ValueAgent(MockProvider(fail=True))
err_result = run(fail_agent.argue("NVDA?", round_num=1, prior_arguments=[], max_tokens=512))
assert err_result.error is not None
assert err_result.verdict == Verdict.HOLD
assert err_result.conviction_score == 50
print("argue() provider failure -> sentinel OK")

# ── 8. argue() — invalid JSON → retries → returns error sentinel ─────────────
bad_agent = ValueAgent(MockProvider(response_text="this is not json at all"))
bad_result = run(bad_agent.argue("NVDA?", round_num=1, prior_arguments=[], max_tokens=512))
assert bad_result.error is not None
assert bad_result.verdict == Verdict.HOLD
print("argue() bad JSON -> error sentinel after retries OK")

# ── 9. _parse_response alias handling (key_risks_to_your_view) ───────────────
raw = VALID_JSON  # uses key_risks_to_your_view
parsed = agent._parse_response(raw, round_num=1, tokens_used=100)
assert parsed.error is None
assert parsed.key_risks_to_view == ["Rising rates"]
print("_parse_response alias OK")

# ── 10. __repr__ ──────────────────────────────────────────────────────────────
r = repr(agent)
assert "ValueAgent" in r
assert "warren" in r
assert "mock" in r
print("__repr__ OK")

print()
print("ALL AGENT LAYER CHECKS PASSED")
