"""
synthesis/synthesizer.py
=========================
Synthesizer — produces the final CommitteeMemo after all debate rounds.

Public surface
--------------
Synthesizer(provider, model)
    .synthesize(thesis, round_history, budget_summary,
                mode_transitions, max_tokens) -> CommitteeMemo
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agents.prompts import SYNTHESIZER_SYSTEM_PROMPT
from models.schemas import (
    AgentArgument,
    AgentFinalPosition,
    BudgetSummary,
    CommitteeMemo,
    ConsensusType,
    DisagreementRecord,
    RoundSummary,
    Verdict,
)
from providers.base import LLMProvider

logger = logging.getLogger(__name__)

class SynthesisOutputSchema(BaseModel):
    """Schema forced on the LLM to guarantee valid output structure."""
    verdict: Verdict
    conviction_score: int = Field(ge=0, le=100)
    consensus_type: ConsensusType
    bull_case: str
    bear_case: str
    key_catalysts: List[str]
    key_risks: List[str]
    recommended_action: str
    position_sizing: str
    conditions_to_revisit: List[str]
    unresolved_flags: List[str]
    agent_positions: List[AgentFinalPosition]

# Verdict numeric rank for weighted-average-to-verdict mapping
_VERDICT_RANK: Dict[Verdict, float] = {
    Verdict.STRONG_BUY:  2.0,
    Verdict.BUY:         1.0,
    Verdict.HOLD:        0.0,
    Verdict.SELL:       -1.0,
    Verdict.STRONG_SELL: -2.0,
}
_RANK_TO_VERDICT: List[tuple] = [
    ( 1.5, Verdict.STRONG_BUY),
    ( 0.5, Verdict.BUY),
    (-0.5, Verdict.HOLD),
    (-1.5, Verdict.SELL),
    (-2.1, Verdict.STRONG_SELL),
]

# Higher quality model for synthesis
_DEFAULT_SYNTHESIS_MODEL = "gemini-2.5-pro-preview-03-25"


class Synthesizer:
    """Produces the final CommitteeMemo using a high-quality LLM.

    Parameters
    ----------
    provider:
        A live :class:`~providers.base.LLMProvider`.
    model:
        Override the synthesis model.  Defaults to ``gemini-2.5-pro``.
        Pass ``None`` to use the provider's own default.
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: Optional[str] = _DEFAULT_SYNTHESIS_MODEL,
    ) -> None:
        self.provider = provider
        self.model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        thesis: str,
        round_history: List[RoundSummary],
        budget_summary: BudgetSummary,
        mode_transitions: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 8192,
    ) -> CommitteeMemo:

        """Generate the final :class:`~models.schemas.CommitteeMemo`.

        1. Builds a rich structured prompt (round summaries, disagreements,
           mode transitions, budget decisions, position-change tracking).
        2. Calls the LLM.
        3. Parses response via ``CommitteeMemo.model_validate_json()``.
        4. Falls back to a rules-based memo if parsing fails — never raises.
        """
        mode_transitions = mode_transitions or []
        prompt = self._build_prompt(
            thesis, round_history, budget_summary, mode_transitions
        )

        try:
            # Support optional model override via kwargs if provider accepts it
            call_kwargs: Dict[str, Any] = dict(
                prompt=prompt,
                max_tokens=max_tokens,
                system=SYNTHESIZER_SYSTEM_PROMPT,
                temperature=0.2,   # low temperature for deterministic synthesis
                response_schema=SynthesisOutputSchema,
            )
            response = await self.provider.complete(**call_kwargs)
            memo = self._parse_response(response.text, thesis, round_history, budget_summary)
        except Exception as exc:
            logger.error("[synthesizer] LLM call failed: %s — building fallback memo", exc)
            memo = self._fallback_memo(thesis, round_history, budget_summary)

        return memo

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        thesis: str,
        rounds: List[RoundSummary],
        budget: BudgetSummary,
        mode_transitions: List[Dict[str, Any]],
    ) -> str:
        lines: List[str] = []

        lines.append(f"INVESTMENT THESIS: {thesis}")
        lines.append("")

        # ── Round-by-round argument summaries ─────────────────────────────
        lines.append("═" * 60)
        lines.append("DEBATE TRANSCRIPT")
        lines.append("═" * 60)

        all_args_by_agent: Dict[str, List[AgentArgument]] = {}
        for r in rounds:
            lines.append(f"\nROUND {r.round_num}  [{r.mode.value.upper()}]  "
                         f"convergence={r.convergence_score:.2f}  "
                         f"tokens={r.tokens_spent_this_round}")
            lines.append("-" * 50)

            for arg in r.arguments:
                if arg.error:
                    lines.append(f"  [{arg.agent_name}] ERROR: {arg.error}")
                    continue
                lines.append(
                    f"  [{arg.agent_name} / {arg.lens}]  "
                    f"{arg.verdict.value}  conviction={arg.conviction_score}/100  "
                    f"tokens={arg.tokens_used}"
                )
                lines.append(f"    {arg.primary_argument}")
                for pt in arg.supporting_points:
                    lines.append(f"    + {pt}")
                for risk in arg.key_risks_to_view:
                    lines.append(f"    ~ Risk: {risk}")
                lines.append(f"    ? Change trigger: {arg.what_would_change_my_mind}")
                if arg.disagreement_with:
                    for target, crux in arg.disagreement_with.items():
                        lines.append(f"    ! Disputes {target}: {crux}")

                # Track per-agent history for position-change detection
                all_args_by_agent.setdefault(arg.agent_id, []).append(arg)

            # Disagreements this round
            if r.disagreements:
                lines.append(f"\n  CONFLICTS ({len(r.disagreements)}):")
                for d in r.disagreements:
                    lines.append(
                        f"    {d.agent_a} vs {d.agent_b}  "
                        f"[{d.conflict_type.value}]: {d.crux[:120]}"
                    )

        # ── Position-change tracking ───────────────────────────────────────
        if len(rounds) > 1:
            lines.append("\nPOSITION CHANGES ACROSS ROUNDS")
            lines.append("-" * 50)
            for agent_id, agent_args in all_args_by_agent.items():
                changes = []
                for i in range(1, len(agent_args)):
                    prev, curr = agent_args[i - 1], agent_args[i]
                    if curr.verdict != prev.verdict or abs(curr.conviction_score - prev.conviction_score) >= 10:
                        changes.append(
                            f"R{prev.round_num}→R{curr.round_num}: "
                            f"{prev.verdict.value}({prev.conviction_score}) "
                            f"→ {curr.verdict.value}({curr.conviction_score})"
                        )
                if changes:
                    lines.append(f"  {agent_args[0].agent_name}: {' | '.join(changes)}")
                else:
                    lines.append(f"  {agent_args[0].agent_name}: position held throughout")

        # ── Mode transitions ───────────────────────────────────────────────
        if mode_transitions:
            lines.append("\nMODE TRANSITIONS")
            lines.append("-" * 50)
            for t in mode_transitions:
                lines.append(
                    f"  Round {t.get('from_round')}: "
                    f"{t.get('from_mode','?').upper()} → {t.get('to_mode','?').upper()}  "
                    f"score={t.get('convergence_score', 0):.1f}"
                )
                if t.get("reason"):
                    lines.append(f"    Reason: {t['reason']}")

        # ── Budget allocation decisions ────────────────────────────────────
        lines.append("\nBUDGET SUMMARY")
        lines.append("-" * 50)
        lines.append(
            f"  Total budget: {budget.total_budget}  |  "
            f"Spent: {budget.total_spent}  |  "
            f"Utilisation: {budget.utilization_pct:.1f}%"
        )
        if budget.extra_allocations_count:
            lines.append(f"  Extra grants (conflict resolution): {budget.extra_allocations_count}")

        # Per-agent token spend
        agent_spend: Dict[str, int] = {}
        for alloc in budget.allocations:
            agent_spend[alloc.agent_id] = (
                agent_spend.get(alloc.agent_id, 0) + alloc.tokens_used
            )
        if agent_spend:
            lines.append("  Per-agent actual spend:")
            for aid, spend in sorted(agent_spend.items(), key=lambda x: -x[1]):
                lines.append(f"    {aid}: {spend} tokens")

        lines.append("")
        lines.append("═" * 60)
        lines.append(
            "Produce the CommitteeMemo JSON exactly matching the schema. "
            "No markdown fences. No preamble. No text outside the JSON object."
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        raw: str,
        thesis: str,
        rounds: List[RoundSummary],
        budget: BudgetSummary,
    ) -> CommitteeMemo:
        """Parse LLM output into CommitteeMemo via model_validate_json."""
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

        # Inject fields the LLM may omit
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Try repair
            try:
                data = json.loads(repair_json(cleaned))
            except json.JSONDecodeError as exc:
                logger.warning("[synthesizer] JSON parse failed (even with repair): %s", exc)
                return self._fallback_memo(thesis, rounds, budget)


        # 3. Recursive unwrap to find the required fields
        _REQUIRED = {"verdict", "conviction_score", "bull_case", "bear_case"}

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

        found = recursive_find(data)
        if found:
            data = found

        # 4. Flatten common nested dicts if LLM misbehaves (e.g. {"bull_case": {"summary": "..."}})
        for field in ["bull_case", "bear_case", "recommended_action", "position_sizing"]:
            val = data.get(field)
            if isinstance(val, dict):
                # Take the first string value found in the dict
                for v in val.values():
                    if isinstance(v, str):
                        data[field] = v
                        break

        # 5. Force override fields the LLM should NOT determine
        data["thesis"] = thesis
        data["budget_summary"] = budget.model_dump()
        data["reasoning_trace"] = [r.model_dump() for r in rounds]


        # 6. Normalize agent_positions if present
        if "agent_positions" in data and isinstance(data["agent_positions"], list):
            for ap in data["agent_positions"]:
                if isinstance(ap, dict):
                    if "reason_for_change" in ap and "change_reason" not in ap:
                        ap["change_reason"] = ap.pop("reason_for_change")
                    if "verdict" in ap and "final_verdict" not in ap:
                        ap["final_verdict"] = ap.pop("verdict")
                    if "conviction" in ap and "final_conviction_score" not in ap:
                        ap["final_conviction_score"] = ap.pop("conviction")

        # Coerce enum strings
        for field_name, enum_cls in [("verdict", Verdict), ("consensus_type", ConsensusType)]:
            val = data.get(field_name)
            if isinstance(val, str):
                try:
                    data[field_name] = enum_cls(val)
                except ValueError:
                    data[field_name] = Verdict.HOLD if field_name == "verdict" else ConsensusType.CONTESTED

        try:
            memo = CommitteeMemo.model_validate(data)
            # Apply deterministic verdict rules — override LLM if needed
            self._apply_verdict_rules(memo, rounds)
            return memo
        except Exception as exc:
            logger.warning("[synthesizer] model_validate failed (%s) — using fallback", exc)
            return self._fallback_memo(thesis, rounds, budget)

    # ------------------------------------------------------------------
    # Verdict determination (exact rules as specified)
    # ------------------------------------------------------------------

    def _apply_verdict_rules(
        self,
        memo: CommitteeMemo,
        rounds: List[RoundSummary],
    ) -> None:
        """Apply deterministic verdict rules, overriding the LLM where required.

        Rules (applied in priority order):
        1. IRRECONCILABLE  → verdict = HOLD (closest to INCONCLUSIVE in the enum)
                             consensus_type confirmed as IRRECONCILABLE
        2. 3+ agents agree → majority verdict, consensus_type = MAJORITY/CONSENSUS
        3. 2-2 split       → CONTESTED; if Solomon argued, use his verdict
        4. conviction_score = tokens-weighted average of final-round agents
        """
        final_args = self._final_round_valid_args(rounds)
        if not final_args:
            return

        # Separate tiebreaker from panel
        panel_args = [a for a in final_args if a.agent_id != "solomon"]
        solomon_args = [a for a in final_args if a.agent_id == "solomon"]

        # ── Rule 1: irreconcilable ─────────────────────────────────────────
        if memo.consensus_type == ConsensusType.IRRECONCILABLE:
            # INCONCLUSIVE is not in Verdict enum — HOLD is the neutral stand-in
            memo.verdict = Verdict.HOLD
            return

        # ── Verdict count from panel ───────────────────────────────────────
        verdict_counts = Counter(a.verdict for a in panel_args)
        most_common_verdict, top_count = verdict_counts.most_common(1)[0]
        n_panel = len(panel_args)

        # ── Rule 2: majority (3+ of 4) ────────────────────────────────────
        if top_count >= 3:
            memo.verdict = most_common_verdict
            memo.consensus_type = (
                ConsensusType.CONSENSUS if top_count == n_panel
                else ConsensusType.MAJORITY
            )

        # ── Rule 3: 2-2 split → CONTESTED, tiebreaker decides ─────────────
        elif n_panel == 4 and top_count == 2 and len(verdict_counts) >= 2:
            memo.consensus_type = ConsensusType.CONTESTED
            if solomon_args:
                memo.verdict = solomon_args[-1].verdict   # Solomon's ruling
            else:
                # No tiebreaker — keep LLM verdict but stamp CONTESTED
                pass

        # ── Rule 4: conviction_score = tokens-weighted average ─────────────
        total_tokens = sum(a.tokens_used for a in panel_args)
        if total_tokens > 0:
            weighted_score = sum(
                a.conviction_score * (a.tokens_used / total_tokens)
                for a in panel_args
            )
            memo.conviction_score = round(weighted_score)
        elif panel_args:
            memo.conviction_score = round(
                sum(a.conviction_score for a in panel_args) / len(panel_args)
            )

    # ------------------------------------------------------------------
    # Fallback memo (no LLM — built from round_history directly)
    # ------------------------------------------------------------------

    def _fallback_memo(
        self,
        thesis: str,
        rounds: List[RoundSummary],
        budget: BudgetSummary,
    ) -> CommitteeMemo:
        """Rules-based CommitteeMemo built without any LLM call."""
        final_args = self._final_round_valid_args(rounds)
        all_disagreements: List[DisagreementRecord] = [
            d for r in rounds for d in r.disagreements
        ]
        panel_args = [a for a in final_args if a.agent_id != "solomon"]
        solomon_args = [a for a in final_args if a.agent_id == "solomon"]

        # Build agent positions
        agent_positions = [
            AgentFinalPosition(
                agent_id=a.agent_id,
                agent_name=a.agent_name,
                lens=a.lens,
                final_verdict=a.verdict,
                final_conviction_score=a.conviction_score,
            )
            for a in panel_args
        ]

        # Apply verdict rules to the fallback memo too
        memo = CommitteeMemo(
            thesis=thesis,
            verdict=Verdict.HOLD,               # will be overridden below
            conviction_score=50,
            consensus_type=ConsensusType.CONTESTED,
            bull_case=self._extract_bull_case(panel_args),
            bear_case=self._extract_bear_case(panel_args),
            key_risks=self._collect_risks(panel_args),
            recommended_action="Manual review required — synthesis LLM failed.",
            position_sizing="Undetermined",
            agent_positions=agent_positions,
            disagreements=all_disagreements,
            budget_summary=budget,
            reasoning_trace=rounds,
        )
        self._apply_verdict_rules(memo, rounds)
        return memo

    # ------------------------------------------------------------------
    # Private utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _final_round_valid_args(rounds: List[RoundSummary]) -> List[AgentArgument]:
        if not rounds:
            return []
        return [a for a in rounds[-1].arguments if not a.error]

    @staticmethod
    def _extract_bull_case(args: List[AgentArgument]) -> str:
        bulls = [
            a for a in args
            if a.verdict in (Verdict.STRONG_BUY, Verdict.BUY)
        ]
        if bulls:
            strongest = max(bulls, key=lambda a: a.conviction_score)
            return f"[{strongest.agent_name}] {strongest.primary_argument}"
        return "No bullish case presented."

    @staticmethod
    def _extract_bear_case(args: List[AgentArgument]) -> str:
        bears = [
            a for a in args
            if a.verdict in (Verdict.STRONG_SELL, Verdict.SELL)
        ]
        if bears:
            strongest = max(bears, key=lambda a: a.conviction_score)
            return f"[{strongest.agent_name}] {strongest.primary_argument}"
        return "No bearish case presented."

    @staticmethod
    def _collect_risks(args: List[AgentArgument]) -> List[str]:
        seen: set = set()
        risks: List[str] = []
        for a in args:
            for risk in a.key_risks_to_view:
                if risk not in seen:
                    risks.append(risk)
                    seen.add(risk)
        return risks[:8]   # top 8 unique risks