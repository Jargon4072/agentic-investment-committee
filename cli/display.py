"""
cli/display.py
==============
DebateDisplay — Rich Live 4-panel debate UI + static synthesis report.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Deque, List, Optional

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from cli.state import AgentState, DisplayState
from models.schemas import CommitteeMemo, ConflictType, ConsensusType, Verdict

# ---------------------------------------------------------------------------
# Colour maps
# ---------------------------------------------------------------------------

_VERDICT_STYLE = {
    "STRONG BUY":  "bold green",
    "BUY":         "green",
    "HOLD":        "yellow",
    "SELL":        "red",
    "STRONG SELL": "bold red",
}
_MODE_STYLE = {"explore": "cyan", "exploit": "bold red"}
_STATUS_ICON = {
    "waiting":  "[dim]-[/dim]",
    "thinking": "[cyan]*[/cyan]",
    "done":     "[green]v[/green]",
    "error":    "[red]x[/red]",
}
_MAX_LOG_LINES = 200


class DebateDisplay:
    """Rich Live UI for the investment committee debate.

    Parameters
    ----------
    thesis:       Investment thesis being debated.
    total_budget: Total token budget.
    max_rounds:   Maximum debate rounds.
    """

    def __init__(self, thesis: str, total_budget: int, max_rounds: int) -> None:
        self.thesis = thesis
        self.total_budget = total_budget
        self.max_rounds = max_rounds
        self._console = Console()
        self._log_lines: Deque[Text] = deque(maxlen=_MAX_LOG_LINES)
        self._state = DisplayState(total_budget=total_budget, max_rounds=max_rounds)
        self._live: Optional[Live] = None
        self._start_time = time.time()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_live(self) -> Live:
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            screen=False,
        )
        return self._live

    def update(self, state: DisplayState) -> None:
        self._state = state
        if self._live:
            self._live.update(self._render())

    def log(self, message: str, style: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = Text()
        line.append(f"[{ts}] ", style="dim")
        line.append(message, style=style)
        self._log_lines.append(line)
        if self._live:
            self._live.update(self._render())

    # ------------------------------------------------------------------
    # Layout renderer
    # ------------------------------------------------------------------

    def _render(self) -> Group:
        s = self._state
        parts = [
            Columns(
                [self._analysts_panel(s), self._status_panel(s)],
                equal=True, expand=True,
            ),
            Columns(
                [self._stream_panel(), self._conflicts_panel(s)],
                equal=True, expand=True,
            ),
            self._budget_footer(s),
        ]
        if s.synthesizer_status != "idle":
            parts.append(self._synthesis_panel(s))
        return Group(*parts)

    # ------------------------------------------------------------------
    # Analysts panel (top-left)
    # ------------------------------------------------------------------

    def _analysts_panel(self, s: DisplayState) -> Panel:
        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="cyan",
            expand=True,
            show_edge=False,
        )
        table.add_column("Agent", style="bold", min_width=22)
        table.add_column("Status", justify="center", width=8)
        table.add_column("Stance", width=12)
        table.add_column("Conf", justify="right", width=5)
        table.add_column("Time", justify="right", width=7)
        table.add_column("Latest argument", no_wrap=False)

        for ag in s.agents:
            v_style = _VERDICT_STYLE.get(ag.verdict, "white")
            icon = _STATUS_ICON.get(ag.status, "")
            elapsed = f"{ag.elapsed_ms/1000:.1f}s" if ag.elapsed_ms else "-"
            snippet = ag.latest_snippet[:60] + "…" if len(ag.latest_snippet) > 60 else ag.latest_snippet
            table.add_row(
                ag.name,
                Text.from_markup(icon),
                Text(ag.verdict or "-", style=v_style),
                str(ag.conviction_score) if ag.verdict else "-",
                elapsed,
                snippet or "-",
            )

        return Panel(
            table,
            title="[bold]Analysts[/bold]",
            border_style="magenta",
            padding=(0, 1),
        )

    # ------------------------------------------------------------------
    # Status panel (top-right)
    # ------------------------------------------------------------------

    def _status_panel(self, s: DisplayState) -> Panel:
        done = sum(1 for a in s.agents if a.status == "done")
        thinking = sum(1 for a in s.agents if a.status == "thinking")
        bulls = sum(1 for a in s.agents if a.verdict in ("STRONG BUY", "BUY"))
        bears = sum(1 for a in s.agents if a.verdict in ("SELL", "STRONG SELL"))
        neutral = sum(1 for a in s.agents if a.verdict == "HOLD")

        mode_style = _MODE_STYLE.get(s.mode, "white")
        conv_bar = "#" * int(s.convergence_score / 10) + "-" * (10 - int(s.convergence_score / 10))

        t = Text()
        t.append("Thesis: ", style="bold")
        t.append(self.thesis[:60] + ("…" if len(self.thesis) > 60 else ""), style="italic")
        t.append("\n\n")
        t.append("Round  : ", style="dim")
        t.append(f"{s.round_num} / {s.max_rounds}\n", style="bold")
        t.append("Mode   : ", style="dim")
        t.append(f"{s.mode.upper()}\n", style=mode_style)
        t.append("Agents : ", style="dim")
        t.append(f"{done} done  {thinking} thinking\n")
        t.append("Split  : ", style="dim")
        t.append(f"{bulls}B ", style="green")
        t.append(f"{bears}S ", style="red")
        t.append(f"{neutral}H\n", style="yellow")
        t.append("Conv.  : ", style="dim")
        t.append(f"[cyan]{conv_bar}[/cyan] {s.convergence_score:.1f}/100\n")

        if s.mode_just_switched:
            t.append("\n[bold yellow]<-> Mode transition[/bold yellow]\n")

        if s.alerts:
            t.append("\n")
            for alert in s.alerts[-4:]:
                t.append(f"! {alert}\n", style="bold red")

        return Panel(t, title="[bold]Status[/bold]", border_style="magenta", padding=(0, 1))

    # ------------------------------------------------------------------
    # Debate stream (bottom-left) — plain Text, no Table
    # ------------------------------------------------------------------

    def _stream_panel(self) -> Panel:
        visible = list(self._log_lines)[-30:]   # last 30 lines
        lines_group = Text("\n").join(visible) if visible else Text("Waiting for debate to start…", style="dim")
        return Panel(
            lines_group,
            title="[bold]Debate Stream[/bold]",
            border_style="yellow",
            padding=(0, 1),
        )

    # ------------------------------------------------------------------
    # Conflicts panel (bottom-right)
    # ------------------------------------------------------------------

    def _conflicts_panel(self, s: DisplayState) -> Panel:
        title = f"[bold]Disagreements ({len(s.disagreements)})[/bold]"
        if not s.disagreements:
            return Panel(
                Text("No conflicts detected", style="dim"),
                title=title, border_style="yellow", padding=(0, 1),
            )

        body = Text()
        for d in s.disagreements[:6]:   # show at most 6 cards
            body.append(f"{d.agent_a} vs {d.agent_b}", style="bold")
            ctype_style = {
                "PHILOSOPHICAL": "magenta",
                "METHODOLOGICAL": "yellow",
                "FACTUAL": "cyan",
                "TIMING": "blue",
            }.get(d.conflict_type.value, "white")
            body.append(f"  [{d.conflict_type.value}]\n", style=ctype_style)
            crux = d.crux[:120] if d.crux else ""
            body.append(f"  {crux}\n", style="dim")
            body.append("  -" * 20 + "\n", style="dim")

        return Panel(body, title=title, border_style="yellow", padding=(0, 1))

    # ------------------------------------------------------------------
    # Budget footer (full-width)
    # ------------------------------------------------------------------

    def _budget_footer(self, s: DisplayState) -> Panel:
        pct = s.tokens_spent / s.total_budget if s.total_budget else 0
        bar_color = "green" if pct < 0.5 else ("yellow" if pct < 0.8 else "red")
        filled = int(pct * 40)
        bar = f"[{bar_color}]{'#' * filled}[/{bar_color}]{'-' * (40 - filled)}"
        label = f" {s.tokens_spent:,} / {s.total_budget:,} tokens  ({pct:.1%})"
        return Panel(
            Text.from_markup(bar + label),
            title="Token Budget",
            border_style=bar_color,
            padding=(0, 1),
        )

    # ------------------------------------------------------------------
    # Synthesis panel (appears when synthesizer starts)
    # ------------------------------------------------------------------

    def _synthesis_panel(self, s: DisplayState) -> Panel:
        if s.synthesizer_status == "running":
            spinner = Spinner("dots", style="cyan")
            body = Text()
            body.append("Synthesizing…\n", style="bold cyan")
            body.append(s.synthesizer_stream_text[-300:], style="dim")
        elif s.synthesizer_status == "complete" and s.final_memo:
            m = s.final_memo
            v_style = _VERDICT_STYLE.get(m.verdict.value, "white")
            body = Text()
            body.append(f"Verdict: ", style="bold")
            body.append(f"{m.verdict.value}  {m.conviction_score}/100\n", style=v_style)
            body.append(f"Consensus: {m.consensus_type.value}\n", style="dim")
            body.append(f"\nBull: ", style="bold green")
            body.append((m.bull_case or "")[:120] + "\n")
            body.append(f"Bear: ", style="bold red")
            body.append((m.bear_case or "")[:120] + "\n")
            body.append(f"\nAction: ", style="bold")
            body.append((m.recommended_action or "")[:120])
        else:
            body = Text("Preparing synthesis…", style="dim")

        return Panel(body, title="[bold cyan]Synthesis[/bold cyan]", border_style="cyan", padding=(0, 1))

    # ------------------------------------------------------------------
    # Static synthesis report (printed after Live exits)
    # ------------------------------------------------------------------

    def print_synthesis(
        self,
        memo: CommitteeMemo,
        output_path: str,
        elapsed: float,
    ) -> None:
        c = self._console

        # ── Final Synthesis rule ───────────────────────────────────────────
        c.print(Rule("[cyan]Final Synthesis[/cyan]"))

        # ── Verdict banner ─────────────────────────────────────────────────
        v_style = _VERDICT_STYLE.get(memo.verdict.value, "white")
        bulls = sum(1 for ap in memo.agent_positions if ap.final_verdict in (Verdict.STRONG_BUY, Verdict.BUY))
        bears = sum(1 for ap in memo.agent_positions if ap.final_verdict in (Verdict.SELL, Verdict.STRONG_SELL))
        n = len(memo.agent_positions)
        split_label = f"{bulls} of {n} agents bullish, {bears} bearish"

        banner = Text(justify="center")
        banner.append(f"\n{memo.verdict.value}\n", style=f"bold {v_style}")
        banner.append(f"Conviction: {memo.conviction_score}/100\n", style="bold")
        banner.append(f"{memo.consensus_type.value}   {split_label}\n", style="dim")
        c.print(Panel(banner, border_style=v_style.split()[-1], padding=(1, 4)))

        # ── Agent positions table ──────────────────────────────────────────
        pos_table = Table(title="Agent Final Positions", box=box.ROUNDED, border_style="magenta")
        pos_table.add_column("Agent", style="bold")
        pos_table.add_column("Lens")
        pos_table.add_column("Verdict")
        pos_table.add_column("Conv", justify="right")
        pos_table.add_column("Changed?")
        pos_table.add_column("Reason")
        for ap in memo.agent_positions:
            v_s = _VERDICT_STYLE.get(ap.final_verdict.value, "white")
            changed = "Yes" if getattr(ap, "position_changed", False) else "No"
            reason = getattr(ap, "change_reason", "") or ""
            pos_table.add_row(
                ap.agent_name,
                ap.lens,
                Text(ap.final_verdict.value, style=v_s),
                str(ap.final_conviction_score),
                changed,
                reason[:60],
            )
        c.print(pos_table)

        # ── Bull / Bear cases ──────────────────────────────────────────────
        c.print(Columns([
            Panel(memo.bull_case or "—", title="[bold green]Bull Case[/bold green]", border_style="green"),
            Panel(memo.bear_case or "—", title="[bold red]Bear Case[/bold red]", border_style="red"),
        ]))

        # ── Catalysts & Risks ──────────────────────────────────────────────
        if memo.key_catalysts:
            cat_text = Text()
            for cat in memo.key_catalysts:
                cat_text.append(f"+ {cat}\n", style="green")
            c.print(Panel(cat_text, title="Key Catalysts", border_style="green"))

        if memo.key_risks:
            risk_text = Text()
            for risk in memo.key_risks:
                risk_text.append(f"~ {risk}\n", style="red")
            c.print(Panel(risk_text, title="Key Risks", border_style="red"))

        # ── Disagreements table ────────────────────────────────────────────
        if memo.disagreements:
            dis_table = Table(title="Disagreements", box=box.ROUNDED, border_style="yellow")
            dis_table.add_column("Agents")
            dis_table.add_column("Type")
            dis_table.add_column("Crux")
            dis_table.add_column("Resolved?")
            for d in memo.disagreements:
                resolved = "Yes" if getattr(d, "resolved", False) else "No"
                res_style = "green" if resolved == "Yes" else "red"
                dis_table.add_row(
                    f"{d.agent_a} vs {d.agent_b}",
                    d.conflict_type.value,
                    (d.crux or "")[:80],
                    Text(resolved, style=res_style),
                )
            c.print(dis_table)

        # ── Unresolved flags (show only if present) ────────────────────────
        if memo.unresolved_flags:
            flag_text = Text()
            for flag in memo.unresolved_flags:
                flag_text.append(f"! {flag}\n", style="bold red")
            c.print(Panel(flag_text, title="[bold red]Unresolved Flags[/bold red]", border_style="red"))

        # ── Recommended action ─────────────────────────────────────────────
        action_text = Text()
        action_text.append("Action:    ", style="bold")
        action_text.append((memo.recommended_action or "—") + "\n")
        action_text.append("Sizing:    ", style="bold")
        action_text.append((memo.position_sizing or "—") + "\n")
        if memo.conditions_to_revisit:
            action_text.append("Revisit if:\n", style="bold")
            for cond in memo.conditions_to_revisit:
                action_text.append(f"  • {cond}\n", style="yellow")
        c.print(Panel(action_text, title="Recommended Action", border_style="cyan"))

        # ── Budget summary ─────────────────────────────────────────────────
        bs = memo.budget_summary
        budget_table = Table(title="Budget Summary", box=box.SIMPLE_HEAD)
        budget_table.add_column("Agent")
        budget_table.add_column("Allocated", justify="right")
        budget_table.add_column("Used", justify="right")
        budget_table.add_column("Delta", justify="right")
        for alloc in bs.allocations:
            delta = alloc.tokens_used - alloc.tokens_allocated
            delta_style = "red" if delta > 0 else "green"
            budget_table.add_row(
                f"{alloc.agent_id} R{alloc.round_num}",
                str(alloc.tokens_allocated),
                str(alloc.tokens_used),
                Text(f"{delta:+d}", style=delta_style),
            )
        budget_table.add_row(
            "[bold]TOTAL[/bold]",
            str(bs.total_budget),
            str(bs.total_spent),
            Text(f"{bs.total_spent - bs.total_budget:+,d}", style="dim"),
        )
        c.print(budget_table)

        # ── Footer ─────────────────────────────────────────────────────────
        rounds_done = len(memo.reasoning_trace)
        c.print(f"\n[bold green]Debate complete.[/bold green]  Trace saved → [cyan]{output_path}[/cyan]")
        c.print(
            f"Total time: [bold]{elapsed:.1f}s[/bold]   "
            f"Tokens used: [bold]{bs.total_spent:,}[/bold]   "
            f"Rounds: [bold]{rounds_done}[/bold]"
        )
