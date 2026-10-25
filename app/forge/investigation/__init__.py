"""Investigation package exports for FORGE."""

from forge.investigation.tools import (
    ToolExecutionError,
    ToolRegistry,
    MAX_EXCERPT_LINES,
    MAX_SEARCH_RESULTS,
    MAX_DIR_ENTRIES,
)
from forge.investigation.loop import (
    InvestigationLoop,
    InvestigationResult,
    SYSTEM_PROMPT,
)

__all__ = [
    "ToolExecutionError",
    "ToolRegistry",
    "MAX_EXCERPT_LINES",
    "MAX_SEARCH_RESULTS",
    "MAX_DIR_ENTRIES",
    "InvestigationLoop",
    "InvestigationResult",
    "SYSTEM_PROMPT",
]
