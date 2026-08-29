"""Versioned prompt loading for the vision path.

Each prompt is a frozen text file under ``vision/prompts``. The on-disk filename
carries the version; loading it also exposes a stable ``id`` which participates in
the job key. A change to the prompt text must bump the filename version, which
automatically changes the job key — stale cached vision results are never reused.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

PARSE_PROMPT_ID = "floorplan_parse_v1"
REVIEW_PROMPT_ID = "floorplan_review_v1"


class PromptNotFound(FileNotFoundError):
    pass


def load_prompt(prompt_id: str) -> str:
    """Return the prompt text for a versioned prompt id."""

    path = _PROMPTS_DIR / f"{prompt_id}.txt"
    if not path.is_file():
        raise PromptNotFound(f"prompt not found: {prompt_id}")
    return path.read_text(encoding="utf-8")


def parse_prompt() -> str:
    return load_prompt(PARSE_PROMPT_ID)


def review_prompt() -> str:
    return load_prompt(REVIEW_PROMPT_ID)
