"""Tests for the methodology: content invariants, SKILL.md parity, and the MCP prompt."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.types import TextContent

from veritas.methodology import INVESTIGATOR_METHODOLOGY, methodology
from veritas.server import METHODOLOGY_PROMPT_NAME, VeritasTools, create_server

if TYPE_CHECKING:
    from veritas.session import InvestigationSession

SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "veritas-investigator" / "SKILL.md"


def test_methodology_states_the_core_discipline() -> None:
    text = methodology()
    assert text == INVESTIGATOR_METHODOLOGY
    assert text.startswith("# Veritas: how to investigate")
    for phrase in ("receipts, or it didn't happen", "hypothesis tree", "Silence is a feature"):
        assert phrase in text


def test_skill_frontmatter_and_body_match_the_methodology() -> None:
    raw = SKILL_PATH.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    _, frontmatter, body = raw.split("---\n", 2)
    assert "name: veritas-investigator" in frontmatter
    assert "description:" in frontmatter
    # the skill body is the canonical methodology verbatim — no drift between the two surfaces
    assert body.strip() == INVESTIGATOR_METHODOLOGY


def test_server_exposes_the_methodology_as_a_prompt(session: InvestigationSession) -> None:
    server = create_server(VeritasTools(session))
    prompts = asyncio.run(server.list_prompts())
    assert [prompt.name for prompt in prompts] == [METHODOLOGY_PROMPT_NAME]
    result = asyncio.run(server.get_prompt(METHODOLOGY_PROMPT_NAME, {}))
    content = result.messages[0].content
    assert isinstance(content, TextContent)
    assert content.text == methodology()
