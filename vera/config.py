"""Runtime configuration. Everything is read from the environment once at
import time so a misconfigured deploy fails loudly on boot, not mid-test."""

import os
from pathlib import Path

try:  # optional: local .env support
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# --- LLM -------------------------------------------------------------------
# anthropic | openai  ("openai" covers any OpenAI-compatible endpoint:
# OpenAI, Groq, Together, OpenRouter, Gemini's /openai endpoint, vLLM, ...)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5")
LLM_MAX_TOKENS = int(_f("LLM_MAX_TOKENS", 900))
LLM_TEMPERATURE = _f("LLM_TEMPERATURE", 0.0)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

# --- Time budgets (judge hard-times-out at 30s) ------------------------------
# The real judge waits 30s; magicpin's local judge_simulator.py waits only 15s
# for /v1/tick and /v1/reply. Defaults are sized for the stricter of the two.
TICK_BUDGET_SECONDS = _f("TICK_BUDGET_SECONDS", 13.0)
REPLY_BUDGET_SECONDS = _f("REPLY_BUDGET_SECONDS", 13.0)
LLM_CALL_TIMEOUT = _f("LLM_CALL_TIMEOUT", 10.0)  # per attempt
BACKGROUND_BUDGET_SECONDS = _f("BACKGROUND_BUDGET_SECONDS", 40.0)  # a composition may outlive one tick
LLM_CONCURRENCY = int(_f("LLM_CONCURRENCY", 6))  # global cap on simultaneous LLM calls
MIN_ATTEMPT_SECONDS = 2.0  # don't start an LLM call with less runway than this
MAX_ACTIONS_PER_TICK = 20
MAX_COMPOSE_ATTEMPTS = 3  # first try + up to two *informed* repairs (time permitting)

# --- Message shape ------------------------------------------------------------
MAX_BODY_CHARS = int(_f("MAX_BODY_CHARS", 640))
MIN_BODY_CHARS = 25

# --- Metadata -------------------------------------------------------------------
TEAM_NAME = os.getenv("TEAM_NAME", "Team Name")
TEAM_MEMBERS = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Member One").split(",") if m.strip()]
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "team@example.com")
BOT_VERSION = "2.0.0"

SYSTEM_PROMPT_PATH = ROOT / "prompts" / "composer_system_prompt.md"
