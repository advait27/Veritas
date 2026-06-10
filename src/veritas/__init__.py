"""Veritas: an MCP server that turns Claude into a rigorous, hypothesis-driven data investigator.

Every numeric claim in any Veritas output must trace to an actually executed
artifact (a SQL or Python result). Verification is deterministic, not LLM-judged.
"""

from typing import Final

__version__: Final = "0.1.0"

DEFAULT_SERVER_NAME: Final = "veritas"
"""Working name for the MCP server; configurable at startup (see DECISIONS.md, D-002)."""
