"""
cli/app.py
===========
Typer CLI for the Investment Committee debate system.

Commands
--------
debate       Run a full multi-round debate.
show-trace   Pretty-print a saved trace JSON.
list-traces  List all saved traces.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import sys
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import typer
from rich.console import Console
from rich.table import Table
from rich import box

app = typer.Typer(
    name="investment-committee",
    help="Multi-agent Investment Committee debate system.",
    add_completion=False,
)
console = Console()
console = Console()


# ---------------------------------------------------------------------------
# debate command
# ---------------------------------------------------------------------------

@app.command()
def debate(
    thesis: str = typer.Argument(..., help="The investment thesis to debate."),
    budget: int = typer.Option(50_000, "--budget", "-b", help="Total token budget."),
    rounds: int = typer.Option(3, "--rounds", "-r", help="Maximum debate rounds."),
    provider: str = typer.Option("gemini", "--provider", "-p", help="LLM provider name."),
    output_dir: str = typer.Option("traces", "--output-dir", "-o", help="Directory for trace JSON files."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Run a multi-round investment committee debate and display results live."""
    import logging
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    asyncio.run(_run_debate(thesis, budget, rounds, provider, output_dir))


async def _run_debate(
    thesis: str,
    budget: int,
    rounds: int,
    provider: str,
    output_dir: str,
) -> None:
    from cli.display import DebateDisplay
    from cli.state import AgentState, DisplayState
    from orchestrator.debate_config import DebateConfig
    from orchestrator.debate_orchestrator import DebateOrchestrator
    from orchestrator.debate_state import DebateState as OrchestratorState
    from models.schemas import DebateMode

    config = DebateConfig(
        thesis=thesis,
        token_budget=budget,
        max_rounds=rounds,
        provider=provider,
        output_dir=output_dir,
    )

    display = DebateDisplay(thesis=thesis, total_budget=budget, max_rounds=rounds)
    orchestrator = DebateOrchestrator(config)

    # Build initial agent state list from panel
    _PANEL = [
        ("warren", "Warren - Value Investor",  "value"),
        ("cathie", "Cathie - Growth Optimist", "growth"),
        ("ray",    "Ray - Macro Strategist",   "macro"),
        ("nassim", "Nassim - Risk Manager",    "risk"),
    ]
    ui_agents = {
        aid: AgentState(name=name, agent_id=aid, lens=lens)
        for aid, name, lens in _PANEL
    }

    state = DisplayState(
        max_rounds=rounds,
        total_budget=budget,
        agents=list(ui_agents.values()),
    )

    start = time.time()
    trace = None
    output_path = ""

    display.log(f"Debate started: [italic]{thesis}[/italic]", "bold")
    display.log(f"Budget: {budget:,} tokens  |  Rounds: {rounds}  |  Provider: {provider}", "dim")

    with display.get_live():
        async for event in orchestrator.stream():
            # Map orchestrator DebateState → UI DisplayState
            state.round_num = event.round_num
            state.mode = event.mode.value
            state.tokens_spent = event.tokens_remaining  # remaining → compute spent
            state.tokens_spent = budget - event.tokens_remaining
            state.convergence_score = event.convergence_score
            state.alerts = []

            if event.event == "agent_done" and event.latest_argument:
                arg = event.latest_argument
                ag = ui_agents.get(arg.agent_id)
                if ag:
                    ag.status = "error" if arg.error else "done"
                    ag.verdict = arg.verdict.value if not arg.error else ""
                    ag.conviction_score = arg.conviction_score
                    ag.latest_snippet = arg.primary_argument[:80]
                    ag.elapsed_ms = 0.0  # orchestrator doesn't track per-agent time yet

                if arg.error:
                    display.log(f"{arg.agent_name}: ERROR — {arg.error}", "bold red")
                else:
                    v_style = {
                        "STRONG BUY": "bold green", "BUY": "green",
                        "HOLD": "yellow", "SELL": "red", "STRONG SELL": "bold red",
                    }.get(arg.verdict.value, "white")
                    display.log(
                        f"{arg.agent_name}: {arg.verdict.value} ({arg.conviction_score}/100)",
                        v_style,
                    )
                    for pt in arg.supporting_points[:2]:
                        display.log(f"  + {pt}", "dim")

                # Mark remaining non-done agents as thinking
                for ag2 in ui_agents.values():
                    if ag2.status == "waiting":
                        ag2.status = "thinking"

            elif event.event == "round_done":
                display.log(
                    f"-- Round {event.round_num} complete  "
                    f"convergence={event.convergence_score:.1f}  "
                    f"mode={event.mode.value.upper()} --",
                    "bold yellow",
                )
                if event.round_summary:
                    state.disagreements = event.round_summary.disagreements
                # Reset agent statuses for next round
                for ag in ui_agents.values():
                    if ag.status == "done":
                        ag.status = "waiting"

            elif event.event == "disagreement":
                state.disagreements = event.disagreements
                state.alerts.append(f"New disagreement detected ({len(event.disagreements)})")
                display.log(
                    f"Disagreement detected: {len(event.disagreements)} conflict(s)", "bold red"
                )

            elif event.event == "tiebreaker":
                state.alerts.append("Solomon spawned to resolve conflict")
                display.log("Solomon (tie-breaker) invoked", "bold magenta")
                if event.latest_argument and not event.latest_argument.error:
                    display.log(
                        f"  Solomon ruling: {event.latest_argument.primary_argument[:100]}",
                        "magenta",
                    )

            elif event.event == "mode_transition":
                state.mode_just_switched = True
                state.alerts.append(
                    f"Mode → {event.mode.value.upper()}: {(event.mode_transition_reason or '')[:60]}"
                )
                display.log(
                    f"MODE SWITCH → {event.mode.value.upper()}", "bold yellow"
                )
            elif event.event == "synthesis_started":
                state.synthesizer_status = "running"
                display.log("Starting synthesis...", "dim")
            else:
                state.mode_just_switched = False

            if event.event == "synthesis_done":
                state.synthesizer_status = "complete"
                state.final_memo = event.final_memo
                display.log("Synthesis complete.", "bold cyan")


            state.agents = list(ui_agents.values())
            display.update(state)

        # Retrieve the saved trace path
        traces = sorted(
            Path(output_dir).glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        output_path = str(traces[0]) if traces else output_dir

    elapsed = time.time() - start

    # Print static synthesis report after Live exits
    if state.final_memo:
        display.print_synthesis(state.final_memo, output_path, elapsed)
    else:
        console.print("[yellow]Synthesis memo not available — check logs.[/yellow]")


# ---------------------------------------------------------------------------
# show-trace command
# ---------------------------------------------------------------------------

@app.command(name="show-trace")
def show_trace(
    path: str = typer.Argument(..., help="Path to a trace JSON file."),
) -> None:
    """Pretty-print a saved debate trace."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    from models.schemas import DebateTrace
    try:
        trace = DebateTrace.model_validate(data)
    except Exception as exc:
        typer.echo(f"Invalid trace file: {exc}", err=True)
        raise typer.Exit(1)

    from cli.display import DebateDisplay
    display = DebateDisplay(
        thesis=trace.thesis,
        total_budget=trace.budget_summary.total_budget,
        max_rounds=len(trace.rounds),
    )
    display.print_synthesis(
        memo=trace.synthesis,
        output_path=path,
        elapsed=0.0,
    )


# ---------------------------------------------------------------------------
# list-traces command
# ---------------------------------------------------------------------------

@app.command(name="list-traces")
def list_traces(
    output_dir: str = typer.Option("traces", "--output-dir", "-o", help="Traces directory."),
) -> None:
    """List all saved debate traces."""
    traces = sorted(Path(output_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not traces:
        console.print("[dim]No traces found.[/dim]")
        return

    table = Table(title=f"Saved Traces ({output_dir})", box=box.ROUNDED)
    table.add_column("File", style="cyan")
    table.add_column("Thesis")
    table.add_column("Verdict")
    table.add_column("Rounds", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Date")

    for p in traces:
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            thesis = d.get("thesis", "—")[:50]
            verdict = d.get("synthesis", {}).get("verdict", "—")
            rounds = len(d.get("rounds", []))
            tokens = d.get("budget_summary", {}).get("total_spent", "—")
            created = d.get("created_at", "—")[:16]
            table.add_row(p.name, thesis, verdict, str(rounds), str(tokens), created)
        except Exception:
            table.add_row(p.name, "[red]unreadable[/red]", "—", "—", "—", "—")

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
