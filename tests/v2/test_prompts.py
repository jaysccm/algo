"""Stage prompts live in v2/prompts/*.md. The Python constants load them, so
any backend that reads the files gets exactly what the API path sends."""
from pathlib import Path

import pytest

from v2 import prompts

PROMPT_FILES = ("classifier", "executor", "changelog", "strategist")


def test_prompts_dir_is_inside_v2():
    assert Path(prompts.__file__).resolve().parent / "prompts" == prompts.PROMPTS_DIR


@pytest.mark.parametrize("name", PROMPT_FILES)
def test_each_prompt_file_loads_non_empty(name):
    text = prompts.load_prompt(name)
    assert len(text) > 200
    assert text == text.strip()


def test_unknown_prompt_raises():
    with pytest.raises(FileNotFoundError):
        prompts.load_prompt("does-not-exist")


def test_classifier_constant_is_the_file():
    from v2.classifier import BATCH_CLASSIFICATION_SYSTEM
    assert prompts.load_prompt("classifier") == BATCH_CLASSIFICATION_SYSTEM


def test_executor_constant_is_the_file():
    from v2.agent import TRADING_SYSTEM_PROMPT
    assert prompts.load_prompt("executor") == TRADING_SYSTEM_PROMPT


def test_changelog_constant_is_the_file_and_demands_strict_json():
    from v2.dashboard_publish import CHANGELOG_SYSTEM_PROMPT
    assert prompts.load_prompt("changelog") == CHANGELOG_SYSTEM_PROMPT
    assert CHANGELOG_SYSTEM_PROMPT.endswith("Return strict JSON only. Do not wrap the response in markdown.")


def test_strategist_file_equals_rendered_session_template():
    from v2.ideation_claude import _STRATEGIST_TEMPLATE, CLAUDE_SESSION_STRATEGIST_SYSTEM
    rendered = _STRATEGIST_TEMPLATE.format(
        timing="before market close",
        review_scope="the portfolio",
        date_ref="today's",
        executor_note=" that runs immediately after you",
    ).strip()
    assert prompts.load_prompt("strategist") == CLAUDE_SESSION_STRATEGIST_SYSTEM
    assert rendered == CLAUDE_SESSION_STRATEGIST_SYSTEM
