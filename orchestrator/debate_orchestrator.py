"""
orchestrator/debate_orchestrator.py
=====================================
DebateOrchestrator — main debate loop.

Public surface
--------------
DebateOrchestrator(config)
    .run(thesis)           -> DebateTrace          (batch)
    .stream(thesis)        -> AsyncIterator[DebateState]  (streaming)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional

from agents.factory import build_all, build_tiebreaker
from agents.base_agent import BaseAgent
from agents.synthesizer import SynthesizerAgent
from models.schemas import (
    AgentArgument, DebateMode, DebateTrace,
    DisagreementRecord, RoundSummary,
)
from orchestrator.budget_manager import BudgetExhaustedError, BudgetManager
from orchestrator.debate_config import DebateConfig
from orchestrator.debate_state import DebateState
from orchestrator.disagreement_detector import DisagreementDetector
from orchestrator.explore_exploit import ExploreExploitEvaluator
from providers.factory import get_provider

logger = logging.getLogger(__name__)

# exploit mode: divide by 0.7 → agents get ~43% more tokens vs explore
_EXPLOIT_BUDGET_DIVISOR: float = 0.7
_PANEL_IDS = ["warren", "cathie", "ray", "nassim"]


class DebateOrchestrator:
    """Manages the full multi-round investment debate.

    Parameters
    ----------
    config:
        A :class:`~orchestrator.debate_config.DebateConfig` instance.
        ``config.thesis`` is the investment thesis.
    """

    def __init__(self, config: DebateConfig) -> None:
        self.config = config
        self._provider = get_provider(config.provider)
        self._agents: Dict[str, BaseAgent] = build_all(self._provider)
        self._panel: List[BaseAgent] = [
            self._agents[aid] for aid in _PANEL_IDS if aid in self._agents
        ]

        self._synthesizer = SynthesizerAgent(self._provider)
        self._budget = BudgetManager(
            total_budget=config.token_budget,
            min_round_budget=config.min_round_budget,
            tiebreaker_reserve=config.tiebreaker_budget,
        )
        self._detector = DisagreementDetector()
        self._evaluator = ExploreExploitEvaluator()

    # ------------------------------------------------------------------
    # Batch run
    # ------------------------------------------------------------------

    async def run(self) -> DebateTrace:
        """Execute the full debate and return a :class:`DebateTrace`.

        Loop:
        1. mode=explore, round_history=[]
        2. For each round 1..max_rounds:
           a. Check budget.can_afford_round() — break if not
           b. tokens_per_agent from mode + remaining budget
           c. Run all panel agents concurrently
           d. Record actual token usage
           e. Detect disagreements
           f. If irreconcilable AND can_afford_tiebreaker → spawn Solomon
           g. Evaluate mode for NEXT round
           h. Append RoundSummary
           i. Log round completion
        3. Run synthesizer
        4. Build DebateTrace
        5. Auto-save to output_dir/{id}.json
        """
        thesis = self.config.thesis
        mode = DebateMode.EXPLORE
        round_history: List[RoundSummary] = []
        mode_transitions: List[dict] = []
        prior_arguments: List[AgentArgument] = []

        for round_num in range(1, self.config.max_rounds + 1):
            remaining_rounds = self.config.max_rounds - round_num + 1

            # a. Budget gate
            tokens_per_agent = self._tokens_per_agent(mode, remaining_rounds)
            if not self._budget.can_afford_round(len(self._panel), tokens_per_agent):
                logger.warning("[orchestrator] budget too low — stopping at round %d", round_num)
                break

            logger.info(
                "[orchestrator] round=%d mode=%s tokens_per_agent=%d remaining=%d",
                round_num, mode.value, tokens_per_agent, self._budget.remaining,
            )

            # b+c. Allocate + run all panel agents concurrently
            round_args = await self._run_round(
                thesis, round_num, prior_arguments, tokens_per_agent
            )

            # d. Record actual usage
            await self._record_usage(round_args)

            # e. Detect disagreements
            disagreements = self._detector.detect(round_args)
            irreconcilable = self._detector.has_irreconcilable(
                disagreements, arguments=round_args
            )

            # f. Tiebreaker if needed
            if irreconcilable and self._budget.can_afford_tiebreaker():
                tb_arg = await self._run_tiebreaker(
                    thesis, round_num, round_args, disagreements
                )
                if tb_arg:
                    round_args.append(tb_arg)
                    logger.info("[orchestrator] tiebreaker spawned round=%d", round_num)

            # g. Evaluate mode for NEXT round
            round_summary = RoundSummary(
                round_num=round_num,
                mode=mode,
                arguments=round_args,
                disagreements=disagreements,
                convergence_score=0.0,
                tokens_spent_this_round=sum(
                    a.tokens_used for a in round_args if not a.error
                ),
            )
            round_history.append(round_summary)

            next_mode, conv_score, components = self._evaluator.evaluate(
                round_history, has_irreconcilable=irreconcilable
            )
            # Stamp convergence_score (0–1 normalised) onto the summary
            round_summary.convergence_score = round(conv_score / 100.0, 4)

            if next_mode != mode:
                reason = self._evaluator.get_transition_reason(
                    conv_score, components.as_dict(),
                    was_explore=(mode == DebateMode.EXPLORE),
                    has_irreconcilable=irreconcilable,
                    is_round_one=(round_num == 1),
                )
                mode_transitions.append({
                    "from_round": round_num,
                    "from_mode": mode.value,
                    "to_mode": next_mode.value,
                    "convergence_score": round(conv_score, 2),
                    "reason": reason,
                })
                logger.info(
                    "[orchestrator] mode transition %s -> %s at round=%d score=%.1f",
                    mode.value, next_mode.value, round_num, conv_score,
                )
                mode = next_mode

            # h. Update prior_arguments for next round
            prior_arguments = round_args

            # i. Log completion
            logger.info(
                "[orchestrator] round=%d done — convergence=%.1f agents=%d disagreements=%d",
                round_num, conv_score, len(round_args), len(disagreements),
            )

        # 3. Synthesize
        budget_summary = self._budget.summary()
        memo = await self._synthesizer.synthesize(
            thesis=thesis,
            round_history=round_history,
            budget_summary=budget_summary,
            mode_transitions=mode_transitions,
        )


        # 4. Build DebateTrace
        trace = DebateTrace(
            thesis=thesis,
            config=self.config.as_dict(),
            rounds=round_history,
            synthesis=memo,
            budget_summary=budget_summary,
            mode_transitions=mode_transitions,
        )

        # 5. Auto-save
        await self._save(trace)
        return trace

    # ------------------------------------------------------------------
    # Streaming run
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncIterator[DebateState]:
        """Same debate loop as run() but yields a DebateState after each event.

        Events yielded:
        - "agent_done"      — after each agent's argue() completes
        - "disagreement"    — after DisagreementDetector fires
        - "tiebreaker"      — after Solomon responds
        - "mode_transition" — when explore/exploit mode changes
        - "round_done"      — after each full round
        - "synthesis_done"  — after CommitteeMemo is built (is_final=True)
        """
        thesis = self.config.thesis
        mode = DebateMode.EXPLORE
        round_history: List[RoundSummary] = []
        mode_transitions: List[dict] = []
        prior_arguments: List[AgentArgument] = []

        for round_num in range(1, self.config.max_rounds + 1):
            remaining_rounds = self.config.max_rounds - round_num + 1
            tokens_per_agent = self._tokens_per_agent(mode, remaining_rounds)

            if not self._budget.can_afford_round(len(self._panel), tokens_per_agent):
                break


            round_args: List[AgentArgument] = []

            # Run agents one at a time to yield per-agent events
            for agent in self._panel:
                granted = await self._budget.allocate(
                    agent.config.id, round_num, tokens_per_agent
                )
                if not granted:
                    continue
                arg = await agent.argue(
                    thesis, round_num, prior_arguments, tokens_per_agent
                )
                await self._budget.record_actual_usage(
                    agent.config.id, round_num, arg.tokens_used
                )
                round_args.append(arg)
                yield DebateState(
                    event="agent_done",
                    round_num=round_num,
                    mode=mode,
                    tokens_remaining=self._budget.remaining,
                    latest_argument=arg,
                )

            # Disagreement detection
            disagreements = self._detector.detect(round_args)
            irreconcilable = self._detector.has_irreconcilable(
                disagreements, arguments=round_args
            )
            if disagreements:
                yield DebateState(
                    event="disagreement",
                    round_num=round_num,
                    mode=mode,
                    tokens_remaining=self._budget.remaining,
                    disagreements=disagreements,
                )

            # Tiebreaker
            if irreconcilable and self._budget.can_afford_tiebreaker():
                tb_arg = await self._run_tiebreaker(
                    thesis, round_num, round_args, disagreements
                )
                if tb_arg:
                    round_args.append(tb_arg)
                    yield DebateState(
                        event="tiebreaker",
                        round_num=round_num,
                        mode=mode,
                        tokens_remaining=self._budget.remaining,
                        latest_argument=tb_arg,
                    )

            round_summary = RoundSummary(
                round_num=round_num,
                mode=mode,
                arguments=round_args,
                disagreements=disagreements,
                convergence_score=0.0,
                tokens_spent_this_round=sum(
                    a.tokens_used for a in round_args if not a.error
                ),
            )
            round_history.append(round_summary)

            next_mode, conv_score, components = self._evaluator.evaluate(
                round_history, has_irreconcilable=irreconcilable
            )
            round_summary.convergence_score = round(conv_score / 100.0, 4)

            if next_mode != mode:
                reason = self._evaluator.get_transition_reason(
                    conv_score, components.as_dict(),
                    was_explore=(mode == DebateMode.EXPLORE),
                    has_irreconcilable=irreconcilable,
                    is_round_one=(round_num == 1),
                )
                mode_transitions.append({
                    "from_round": round_num,
                    "from_mode": mode.value,
                    "to_mode": next_mode.value,
                    "convergence_score": round(conv_score, 2),
                    "reason": reason,
                })
                yield DebateState(
                    event="mode_transition",
                    round_num=round_num,
                    mode=next_mode,
                    convergence_score=conv_score,
                    tokens_remaining=self._budget.remaining,
                    mode_transition_reason=reason,
                )
                mode = next_mode

            prior_arguments = round_args
            yield DebateState(
                event="round_done",
                round_num=round_num,
                mode=mode,
                convergence_score=conv_score,
                tokens_remaining=self._budget.remaining,
                round_summary=round_summary,
                disagreements=disagreements,
            )

        # Synthesis
        yield DebateState(
            event="synthesis_started",
            tokens_remaining=self._budget.remaining,
        )
        budget_summary = self._budget.summary()
        memo = await self._synthesizer.synthesize(

            thesis=thesis,
            rounds=round_history,
            budget_summary=budget_summary,
            max_tokens=4_000,
        )
        trace = DebateTrace(
            thesis=thesis,
            config=self.config.as_dict(),
            rounds=round_history,
            synthesis=memo,
            budget_summary=budget_summary,
            mode_transitions=mode_transitions,
        )
        await self._save(trace)
        yield DebateState(
            event="synthesis_done",
            round_num=self.config.max_rounds,
            mode=mode,
            tokens_remaining=self._budget.remaining,
            is_final=True,
            final_memo=memo,
        )


    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _tokens_per_agent(self, mode: DebateMode, remaining_rounds: int) -> int:
        """Compute per-agent token budget for one round.
        Leaves room for tiebreaker reserve and synthesis.
        """
        n = len(self._panel)
        if n == 0: return 0

        # Effective budget available for debate rounds
        # Subtract reserves so we don't hit the gate floor
        available = self._budget.remaining - self.config.tiebreaker_budget - 4000  # 4k for synthesis
        available = max(available, 0)

        base = available // max(remaining_rounds * n, 1)
        if mode == DebateMode.EXPLOIT:
            base = int(base / _EXPLOIT_BUDGET_DIVISOR)

        return max(base, self.config.min_round_budget)


    async def _run_round(
        self,
        thesis: str,
        round_num: int,
        prior_arguments: List[AgentArgument],
        tokens_per_agent: int,
    ) -> List[AgentArgument]:
        """Allocate and run all panel agents concurrently."""
        tasks = []
        allocated_agents: List[BaseAgent] = []
        for agent in self._panel:
            try:
                granted = await self._budget.allocate(
                    agent.config.id, round_num, tokens_per_agent
                )
                if granted:
                    allocated_agents.append(agent)
                    tasks.append(
                        agent.argue(thesis, round_num, prior_arguments, tokens_per_agent)
                    )
            except BudgetExhaustedError:
                logger.warning(
                    "[orchestrator] budget exhausted before allocating %s round=%d",
                    agent.config.id, round_num,
                )
                break
        results = await asyncio.gather(*tasks, return_exceptions=True)
        args: List[AgentArgument] = []
        for agent, result in zip(allocated_agents, results):
            if isinstance(result, AgentArgument):
                args.append(result)
            else:
                logger.error(
                    "[orchestrator] agent %s raised: %s", agent.config.id, result
                )
        return args

    async def _record_usage(self, args: List[AgentArgument]) -> None:
        for arg in args:
            if not arg.error:
                await self._budget.record_actual_usage(
                    arg.agent_id, arg.round_num, arg.tokens_used
                )

    async def _run_tiebreaker(
        self,
        thesis: str,
        round_num: int,
        round_args: List[AgentArgument],
        disagreements: List[DisagreementRecord],
    ) -> Optional[AgentArgument]:
        """Spawn Solomon with a conflict-description prompt."""
        tb = build_tiebreaker(self._provider)
        crux_summary = "; ".join(
            f"{d.agent_a} vs {d.agent_b} [{d.conflict_type.value}]: {d.crux[:80]}"
            for d in disagreements[:3]
        )
        conflict_thesis = (
            f"The committee has reached irreconcilable disagreement on: {thesis}\n\n"
            f"Key conflicts:\n{crux_summary}\n\n"
            f"Classify each conflict, identify the crux, and make a ruling."
        )
        tb_budget = self.config.tiebreaker_budget
        try:
            granted = await self._budget.allocate_extra(
                "solomon", round_num, tb_budget, reason="tiebreaker"
            )
            if not granted:
                return None
            arg = await tb.argue(conflict_thesis, round_num, round_args, tb_budget)
            await self._budget.record_actual_usage("solomon", round_num, arg.tokens_used)
            return arg
        except Exception as exc:
            logger.error("[orchestrator] tiebreaker failed: %s", exc)
            return None

    async def _save(self, trace: DebateTrace) -> None:
        """Persist trace as JSON to output_dir/{id}.json."""
        try:
            os.makedirs(self.config.output_dir, exist_ok=True)
            path = os.path.join(self.config.output_dir, f"{trace.id}.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(trace.model_dump_json(indent=2))
            logger.info("[orchestrator] trace saved → %s", path)
        except Exception as exc:
            logger.error("[orchestrator] failed to save trace: %s", exc)