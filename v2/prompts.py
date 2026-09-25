"""Stage prompt text, loaded from v2/prompts/<name>.md.

The Python constants (BATCH_CLASSIFICATION_SYSTEM, TRADING_SYSTEM_PROMPT,
CHANGELOG_SYSTEM_PROMPT, CLAUDE_SESSION_STRATEGIST_SYSTEM) are loaded from
these files, so any other backend that reads the same files sends exactly
what the API path sends (tests/v2/test_prompts.py).

v2/ is mounted at /app/v2 in the trading container, so the files resolve
there without a compose change.
"""
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    """System prompt for a stage (`v2/prompts/<name>.md`), stripped."""
    return (PROMPTS_DIR / f"{name}.md").read_text().strip()
