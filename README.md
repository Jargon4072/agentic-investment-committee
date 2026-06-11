# AI Investment Committee — Multi-Agent Debate System

A high-performance, budget-constrained autonomous investment committee that simulates institutional decision-making through multi-agent debate, dynamic explore-exploit reasoning, and structured synthesis.

## 🚀 Overview

Institutional investment decisions are rarely made in isolation. They are the result of rigorous debate between analysts with conflicting priorities and specialized lenses. This system digitizes that process using a panel of distinct AI personas, a dynamic orchestrator, and a real-time terminal dashboard.

---

## 🧠 Architecture Decisions

### 1. The Analyst Panel (Lenses)
Instead of simple temperature variance, the system uses four distinct analyst personas with hard-coded "blind spots" and priorities:
*   **Warren (Value)**: Focuses on FCF yields and moats; ignores macro noise.
*   **Cathie (Growth)**: Prioritizes S-curve inflection and TAM; ignores valuation multiples.
*   **Ray (Macro)**: Driven by liquidity cycles and credit impulses; ignores individual stock moats.
*   **Nassim (Risk)**: Paranoid about tail risks and concavity; ignores "perfect execution" narratives.

### 2. Dynamic Orchestration (Explore-Exploit)
The `DebateOrchestrator` does not use a fixed round count. It evaluates the "state of the debate" after each round using a `ConvergenceEvaluator`:
*   **EXPLORE Mode**: In early rounds or when disagreement is high, budget is allocated to maximize diversity of thought.
*   **EXPLOIT Mode**: When the system detects "clusters of agreement" or specific points of contention, it shifts budget to deepening those specific threads and forces agents to confront each other's arguments.

### 3. Budget-Constrained Reasoning
Every debate has a hard **Token Budget**. The `BudgetManager` treats tokens as a finite resource:
*   Allocations are calculated based on remaining rounds and agent importance.
*   **Solomon (Tie-Breaker)** is only spawned if the disagreement is classified as "Methodological" or "Factual" and budget permits.

### 4. Resilient Parsing Layer
LLMs frequently fail to produce perfect JSON when under high pressure or token limits. I implemented a two-stage resilient parser:
*   **Recursive Unwrapping**: Locates the target schema even if the LLM wraps it in nested prose or "thought" blocks.
*   **Auto-Repair**: A logic-based JSON fixer that can close truncated strings, brackets, and braces if a response is cut off by token limits.

---

## 🖥️ Real-Time Terminal UI

The system features a high-fidelity terminal dashboard built with **Python's Rich library**, providing real-time visibility into the "thinking" process of the committee.
<img width="1105" height="677" alt="image" src="https://github.com/user-attachments/assets/d881d1de-c006-42d6-867e-7df7c0d0a6b8" />
<img width="1030" height="990" alt="image" src="https://github.com/user-attachments/assets/8d14b081-b3d2-4265-a001-c28bc3f98a87" />
<img width="1030" height="1042" alt="image" src="https://github.com/user-attachments/assets/b3792578-0ca4-4bf7-a5db-e554f910d879" />


### 🎨 UI Components
*   **Analysts Panel**: A live table tracking all 4 agents. Includes status indicators (spinners for active thinking), color-coded stances (Bullish/Bearish/Hold), conviction scores, and live snippets of their arguments.
*   **Debate Status**: Real-time telemetry showing the current round, orchestration mode (Explore/Exploit), agent stance distribution (Bull/Bear split), and the calculated convergence score.
*   **Live Debate Stream**: A scrolling feed of full arguments as they are parsed, including system alerts for mode transitions, tie-breaker invocations, and budget adjustments.
*   **Conflict Tracker**: A dedicated panel that isolates and displays detected disagreements between specific agents, categorized by type (Factual, Methodological, Philosophical, or Timing).
*   **Budget Telemetry**: A full-width footer with a progress bar tracking token consumption against the total debate budget.

### 🛠️ How It's Built
*   **Engine**: Built using `Rich.live` and `Rich.layout` to maintain a persistent, non-flickering dashboard while the agents reason in the background.
*   **Async Event Loop**: The orchestrator yields `DebateState` events via an `AsyncIterator`. The CLI loop consumes these events and triggers an immediate re-render of the dashboard.
*   **Cross-Platform Rendering**: Implements global UTF-8 encoding patches and ASCII-safe boundary rendering (using `box.SIMPLE_HEAD`) to ensure stability across CMD, PowerShell, and modern terminal emulators like Windows Terminal or iTerm2.

---

## 🛠 What was Refactored & Fixed

*   **Gemini Schema Sanitization**: The Gemini API (google-genai) explicitly forbids `additionalProperties` in JSON schemas. I refactored the `GeminiProvider` to recursively strip these from Pydantic-generated schemas.
*   **Windows Console Stability**: Fixed `UnicodeEncodeError` crashes on Windows by forcing UTF-8 encoding across `stdout` and `stderr` and sanitizing agent display names.
*   **Budget Starvation**: Refactored the orchestrator's allocation logic to ensure a "Synthesis Reserve" is always maintained, preventing the system from running out of tokens right before producing the final memo.
*   **State Propagation**: Fixed a bug where the `final_memo` was lost during the transition from the background orchestrator to the Rich UI display.

---

## ⚖️ Tradeoffs

*   **Fallback vs. Failure**: When the LLM fails to produce a valid synthesis memo despite retries, the system generates a **Rules-Based Fallback Memo** from the raw round history. I prioritized system reliability (getting a report) over strict LLM-generated nuance.
*   **Real-time Latency**: Using multiple agents in sequence/parallel with structured output adds latency. I mitigated this with a **Rich Live Dashboard** that streams status events (thinking, talking, resolving) so the user is never stuck at a static prompt.
*   **Token Accuracy**: The budget manager is "soft" — it allows agents to finish their thought even if they slightly exceed their grant (to prevent truncated JSON), but deducts the overage from future rounds.

---
---

## 📋 Setup & Usage

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Environment**:
    ```bash
    cp .env.example .env
    # Add your GEMINI_API_KEY
    ```


2.  **Run Debate**:
    ```bash
    python -m cli.app debate "Is NVIDIA fairly valued at current prices?" --budget 50000 --rounds 2
    ```

3.  **View Traces**:
    All debate traces are saved to the `./traces` directory as structured JSON for post-mortem analysis.

---

## 🧪 Testing
The core logic is covered by smoke tests and validation suites:
```bash
pytest test_schemas_smoke.py
```
