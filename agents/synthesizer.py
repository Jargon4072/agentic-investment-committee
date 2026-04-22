"""
agents/synthesizer.py
======================
SynthesizerAgent — produces CommitteeMemo from the full round history.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import List, Optional

from models.schemas import (
    AgentFinalPosition, BudgetSummary, CommitteeMemo, ConsensusType,
    DisagreementRecord, RoundSummary, Verdict,
)
from providers.base import LLMProvider
from agents.prompts import SYNTHESIZER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_FALLBACK_MEMO_TEXT = "[Synthesis failed — see error log]"


class SynthesizerAgent:
    """Calls the LLM with the full debate history and parses CommitteeMemo."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def synthesize(
        self,
        thesis: str,
        rounds: List[RoundSummary],
        budget_summary: BudgetSummary,
        max_tokens: int = 4_000,
    ) -> CommitteeMemo:
        prompt = self._build_prompt(thesis, rounds, budget_summary)
        try:
            response = await self.provider.complete(
                prompt=prompt,
                max_tokens=max_tokens,
                system=SYNTHESIZER_SYSTEM_PROMPT,
                temperature=0.3,   # low temp for deterministic synthesis
            )
            return self._parse(response.text, thesis, rounds, budget_summary)
        except Exception as exc:
            logger.error("[synthesizer] failed: %s", exc)
            return self._fallback(thesis, rounds, budget_summary)

    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        thesis: str,
        rounds: List[RoundSummary],
        budget_summary: BudgetSummary,
    ) -> str:
        lines = [f"INVESTMENT THESIS: {thesis}", ""]
        for r in rounds:
            lines.append(f"=== Round {r.round_num} [{r.mode.value.upper()}] ===")
            for arg in r.arguments:
                if arg.error:
                    lines.append(f"  {arg.agent_name}: ERROR — {arg.error}")
                    continue
                lines.append(
                    f"  {arg.agent_name} ({arg.lens}): {arg.verdict.value} "
                    f"({arg.conviction_score}/100)"
                )
                lines.append(f"    {arg.primary_argument}")
                for risk in arg.key_risks_to_view:
                    lines.append(f"    Risk: {risk}")
            if r.disagreements:
                lines.append(f"  Conflicts: {len(r.disagreements)}")
                for d in r.disagreements:
                    lines.append(f"    {d.agent_a} vs {d.agent_b} [{d.conflict_type.value}]: {d.crux[:100]}")
            lines.append(f"  Convergence: {r.convergence_score:.2f}")
            lines.append("")
        lines.append(
            f"Budget: {budget_summary.total_spent}/{budget_summary.total_budget} tokens "
            f"({budget_summary.utilization_pct:.1f}%)"
        )
        lines.append("")
        lines.append(
            "Produce CommitteeMemo JSON exactly matching the schema. "
            "No markdown fences. No preamble."
        )
        return "\n".join(lines)

    def _parse(
        self,
        raw: str,
        thesis: str,
        rounds: List[RoundSummary],
        budget_summary: BudgetSummary,
    ) -> CommitteeMemo:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip()).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning("[synthesizer] JSON parse failed: %s", exc)
            return self._fallback(thesis, rounds, budget_summary)

        # Inject fields the LLM can't know
        data.setdefault("thesis", thesis)
        data.setdefault("budget_summary", budget_summary.model_dump())
        data.setdefault("reasoning_trace", [r.model_dump() for r in rounds])

        # Best-effort enum coercion for verdict
        if "verdict" in data and isinstance(data["verdict"], str):
            try:
                data["verdict"] = Verdict(data["verdict"])
            except ValueError:
                data["verdict"] = Verdict.HOLD

        if "consensus_type" in data and isinstance(data["consensus_type"], str):
            try:
                data["consensus_type"] = ConsensusType(data["consensus_type"])
            except ValueError:
                data["consensus_type"] = ConsensusType.CONTESTED

        try:
            return CommitteeMemo.model_validate(data)
        except Exception as exc:
            logger.warning("[synthesizer] model_validate failed: %s", exc)
            return self._fallback(thesis, rounds, budget_summary)

    def _fallback(
        self,
        thesis: str,
        rounds: List[RoundSummary],
        budget_summary: BudgetSummary,
    ) -> CommitteeMemo:
        """Return a minimal valid CommitteeMemo when synthesis fails."""
        all_args = [a for r in rounds for a in r.arguments if a.error is None]
        all_disagreements = [d for r in rounds for d in r.disagreements]

        # Derive verdict from majority of final round's valid args
        final_args = [a for a in (rounds[-1].arguments if rounds else []) if not a.error]
        if final_args:
            from collections import Counter
            majority_verdict = Counter(a.verdict for a in final_args).most_common(1)[0][0]
            avg_conviction = int(sum(a.conviction_score for a in final_args) / len(final_args))
        else:
            majority_verdict = Verdict.HOLD
            avg_conviction = 50

        agent_positions = [
            AgentFinalPosition(
                agent_id=a.agent_id,
                agent_name=a.agent_name,
                lens=a.lens,
                final_verdict=a.verdict,
                final_conviction_score=a.conviction_score,
            )
            for a in final_args
        ]

        return CommitteeMemo(
            thesis=thesis,
            verdict=majority_verdict,
            conviction_score=avg_conviction,
            consensus_type=ConsensusType.CONTESTED,
            bull_case="[Synthesis failed — manual review required]",
            bear_case="[Synthesis failed — manual review required]",
            recommended_action="[Manual review required]",
            position_sizing="[Undetermined]",
            agent_positions=agent_positions,
            disagreements=all_disagreements,
            budget_summary=budget_summary,
            reasoning_trace=rounds,
        )
