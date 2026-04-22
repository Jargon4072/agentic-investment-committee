"""
orchestrator/disagreement_detector.py
======================================
Detects and classifies conflicts between analyst agents after each debate round.

Public surface
--------------
DisagreementDetector
    .detect(arguments)            -> list[DisagreementRecord]
    .has_irreconcilable(records)  -> bool
    .get_most_contested_agents(records) -> Optional[tuple[str, str]]
"""

from __future__ import annotations

import logging
from collections import Counter
from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

from models.schemas import AgentArgument, ConflictType, DisagreementRecord, Verdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verdict polarity helpers
# ---------------------------------------------------------------------------

# Numeric rank used to measure how far apart two verdicts are.
# Span of 3+ = hard conflict; 2 = soft divergence.
_VERDICT_RANK: Dict[Verdict, int] = {
    Verdict.STRONG_BUY:  2,
    Verdict.BUY:         1,
    Verdict.HOLD:        0,
    Verdict.SELL:       -1,
    Verdict.STRONG_SELL: -2,
}

# Pairs whose rank difference >= this threshold are "hard conflicts"
_HARD_CONFLICT_RANK_GAP: int = 3   # e.g. STRONG BUY (2) vs SELL (-1) = 3

# Conviction score spread that, combined with verdict divergence, flags a conflict
_SCORE_SPREAD_THRESHOLD: int = 35

# Spread across ALL agents that indicates irreconcilable state
_IRRECONCILABLE_SPREAD: int = 60

# Min PHILOSOPHICAL conflicts to declare irreconcilable
_PHILOSOPHICAL_IRRECONCILABLE: int = 2


# ---------------------------------------------------------------------------
# DisagreementDetector
# ---------------------------------------------------------------------------


class DisagreementDetector:
    """Stateless detector: call :meth:`detect` after each round completes.

    All methods are synchronous — no I/O, no side effects.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        arguments: List[AgentArgument],
    ) -> List[DisagreementRecord]:
        """Scan *arguments* and return every detected conflict.

        Detection rules (applied in order; no duplicate pairs):

        1. **HARD CONFLICT** — opposite verdicts:
           STRONG BUY vs SELL/STRONG SELL, or BUY vs STRONG SELL.
           Always creates a ``DisagreementRecord``.

        2. **SCORE CONFLICT** — conviction spread > 35 points *and*
           verdicts also differ.  Creates a ``DisagreementRecord``.

        3. **EXPLICIT DISAGREEMENT** — an agent's ``disagreement_with``
           field names another agent in the panel.
           Always creates a ``DisagreementRecord``.

        Conflict-type classification (applied to each pair):

        - Different lens types (e.g. ``"value"`` vs ``"growth"``)
          → ``METHODOLOGICAL``
        - Same lens → ``FACTUAL``
        - One agent's ``what_would_change_my_mind`` contains a key phrase
          from the other's ``primary_argument`` (simple substring heuristic)
          → ``PHILOSOPHICAL``  (overrides METHODOLOGICAL/FACTUAL)
        - Default → ``METHODOLOGICAL``

        Parameters
        ----------
        arguments:
            All :class:`~models.schemas.AgentArgument` objects from the
            current round.  Error sentinels (``arg.error is not None``)
            are **skipped** — a failing agent cannot generate a conflict.

        Returns
        -------
        list[DisagreementRecord]
            Unique per agent-pair. Empty if no conflicts detected.
        """
        # Filter out error sentinels — they carry no meaningful signal
        valid: List[AgentArgument] = [a for a in arguments if a.error is None]

        if len(valid) < 2:
            return []

        records: List[DisagreementRecord] = []
        seen_pairs: Set[Tuple[str, str]] = set()

        # Build a lookup so explicit-disagreement rule can find named peers
        by_id: Dict[str, AgentArgument] = {a.agent_id: a for a in valid}
        by_name: Dict[str, AgentArgument] = {a.agent_name: a for a in valid}

        # ── Rules 1 & 2: verdict / score conflicts ────────────────────────
        for a, b in combinations(valid, 2):
            pair = self._pair_key(a.agent_id, b.agent_id)
            if pair in seen_pairs:
                continue

            rank_a = _VERDICT_RANK[a.verdict]
            rank_b = _VERDICT_RANK[b.verdict]
            rank_gap = abs(rank_a - rank_b)
            score_spread = abs(a.conviction_score - b.conviction_score)
            verdicts_differ = a.verdict != b.verdict

            is_hard   = rank_gap >= _HARD_CONFLICT_RANK_GAP
            is_score  = score_spread > _SCORE_SPREAD_THRESHOLD and verdicts_differ

            if is_hard or is_score:
                crux = self._infer_crux(a, b)
                conflict_type = self._classify(a, b)
                records.append(DisagreementRecord(
                    agent_a=a.agent_id,
                    agent_b=b.agent_id,
                    conflict_type=conflict_type,
                    crux=crux,
                ))
                seen_pairs.add(pair)
                logger.debug(
                    "[disagreement] %s detected — %s vs %s (verdicts: %s/%s, spread: %d)",
                    "HARD" if is_hard else "SCORE",
                    a.agent_id, b.agent_id,
                    a.verdict.value, b.verdict.value,
                    score_spread,
                )

        # ── Rule 3: explicit disagreement_with references ─────────────────
        for arg in valid:
            for ref_name, contention in arg.disagreement_with.items():
                # The ref may be an agent_id or display name
                peer = by_id.get(ref_name) or by_name.get(ref_name)
                if peer is None:
                    # Named agent not in this round's panel — skip
                    continue
                pair = self._pair_key(arg.agent_id, peer.agent_id)
                if pair in seen_pairs:
                    continue

                conflict_type = self._classify(arg, peer)
                crux = contention or self._infer_crux(arg, peer)
                records.append(DisagreementRecord(
                    agent_a=arg.agent_id,
                    agent_b=peer.agent_id,
                    conflict_type=conflict_type,
                    crux=crux,
                ))
                seen_pairs.add(pair)
                logger.debug(
                    "[disagreement] EXPLICIT — %s disputes %s: %r",
                    arg.agent_id, peer.agent_id, crux[:80],
                )

        logger.info(
            "[disagreement] detect() found %d conflict(s) from %d arguments",
            len(records), len(valid),
        )
        return records

    def has_irreconcilable(
        self,
        records: List[DisagreementRecord],
        arguments: Optional[List[AgentArgument]] = None,
    ) -> bool:
        """Return ``True`` if the committee is in an irreconcilable state.

        Irreconcilable conditions (any one is sufficient):

        * 2+ ``PHILOSOPHICAL`` conflicts in *records*.
        * All agents in *arguments* have distinct verdicts  (requires
          *arguments* to be passed; ignored if ``None``).
        * Conviction score spread across all agents > 60 points  (requires
          *arguments*).

        Parameters
        ----------
        records:
            ``DisagreementRecord`` list from the current round.
        arguments:
            Optional list of :class:`AgentArgument` for the same round.
            Needed for the all-distinct-verdicts and score-spread checks.
        """
        # Condition 1 — philosophical count
        philosophical = sum(
            1 for r in records if r.conflict_type == ConflictType.PHILOSOPHICAL
        )
        if philosophical >= _PHILOSOPHICAL_IRRECONCILABLE:
            logger.info(
                "[disagreement] irreconcilable — %d PHILOSOPHICAL conflicts", philosophical
            )
            return True

        if arguments:
            valid = [a for a in arguments if a.error is None]

            # Condition 2 — all agents hold distinct verdicts
            verdicts = [a.verdict for a in valid]
            if len(verdicts) >= 4 and len(set(verdicts)) == len(verdicts):
                logger.info(
                    "[disagreement] irreconcilable — all %d agents have distinct verdicts: %s",
                    len(verdicts), [v.value for v in verdicts],
                )
                return True

            # Condition 3 — score spread > 60
            if valid:
                scores = [a.conviction_score for a in valid]
                spread = max(scores) - min(scores)
                if spread > _IRRECONCILABLE_SPREAD:
                    logger.info(
                        "[disagreement] irreconcilable — score spread %d > %d",
                        spread, _IRRECONCILABLE_SPREAD,
                    )
                    return True

        return False

    def get_most_contested_agents(
        self,
        records: List[DisagreementRecord],
    ) -> Optional[Tuple[str, str]]:
        """Return the agent pair that appears in the most conflict records.

        Parameters
        ----------
        records:
            List of :class:`~models.schemas.DisagreementRecord`.

        Returns
        -------
        tuple[str, str] or None
            The ``(agent_a_id, agent_b_id)`` pair with the highest conflict
            count, in the canonical alphabetical order used internally.
            Returns ``None`` if *records* is empty or all pairs are tied
            at zero conflicts.
        """
        if not records:
            return None

        pair_counts: Counter = Counter()
        for r in records:
            pair_counts[self._pair_key(r.agent_a, r.agent_b)] += 1

        if not pair_counts:
            return None

        most_common_key, _ = pair_counts.most_common(1)[0]
        a_id, b_id = most_common_key  # tuple stored by _pair_key
        return (a_id, b_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pair_key(id_a: str, id_b: str) -> Tuple[str, str]:
        """Return a canonical (alphabetically sorted) pair key."""
        return (min(id_a, id_b), max(id_a, id_b))

    @staticmethod
    def _classify(a: AgentArgument, b: AgentArgument) -> ConflictType:
        """Classify the conflict type between agents *a* and *b*.

        Priority order:
        1. PHILOSOPHICAL — if either agent's ``what_would_change_my_mind``
           contains a meaningful phrase (>4 words) that appears verbatim in
           the other's ``primary_argument``.
        2. METHODOLOGICAL — if the agents have different lenses.
        3. FACTUAL — same lens (rare but possible in multi-round debates).
        """
        # PHILOSOPHICAL heuristic: flip-condition overlaps with other's core thesis
        for agent, other in [(a, b), (b, a)]:
            wcmm_words = agent.what_would_change_my_mind.lower().split()
            # Extract ngrams of length 4+ from what_would_change_my_mind
            for n in range(4, min(len(wcmm_words) + 1, 9)):
                for i in range(len(wcmm_words) - n + 1):
                    phrase = " ".join(wcmm_words[i : i + n])
                    if phrase in other.primary_argument.lower():
                        return ConflictType.PHILOSOPHICAL

        # METHODOLOGICAL vs FACTUAL based on lens
        if a.lens != b.lens:
            return ConflictType.METHODOLOGICAL

        return ConflictType.FACTUAL

    @staticmethod
    def _infer_crux(a: AgentArgument, b: AgentArgument) -> str:
        """Produce a short human-readable crux description for the conflict.

        Uses the agents' verdicts and primary arguments to construct a
        one-sentence description of the core disagreement.
        """
        return (
            f"{a.agent_name} ({a.verdict.value}/{a.conviction_score}) vs "
            f"{b.agent_name} ({b.verdict.value}/{b.conviction_score}): "
            f"'{a.primary_argument[:80]}...' "
            f"vs '{b.primary_argument[:80]}...'"
        )
