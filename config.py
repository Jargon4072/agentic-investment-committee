"""
config.py
=========
Central configuration loader.

Reads from the .env file (if present) via python-dotenv,
then exposes the two primary settings as module-level constants.

Usage
-----
>>> from config import GEMINI_API_KEY, LLM_MODEL
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (the directory containing this file)
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)

# ---------------------------------------------------------------------------
# Resolved values (read once at import time)
# ---------------------------------------------------------------------------

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
"""Google AI Studio / Vertex API key.  Required for GeminiProvider."""

LLM_MODEL: str = os.environ.get("LLM_MODEL", "gemini/gemini-2.5-flash")
"""Default LLM model string.  Format: 'provider/model-name'."""

# Strip the provider prefix if present (google-genai SDK only wants the model name)
# e.g. "gemini/gemini-2.5-flash" -> "gemini-2.5-flash"
_model_parts = LLM_MODEL.split("/", 1)
GEMINI_MODEL: str = _model_parts[-1]
"""Bare Gemini model name for direct use with google-genai SDK."""
