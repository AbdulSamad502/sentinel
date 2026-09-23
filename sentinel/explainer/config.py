"""How much of a change the explainer is willing to put in front of the model.

These are token budgets, not policy. A session that rewrites forty files would
cost a fortune to narrate in full and would read worse for it, so the prompt
gets the shape of the change and a bounded slice of each diff.

Anything trimmed is marked in the text the model sees, so it is never misled
into describing a partial diff as the whole change.
"""

from dataclasses import dataclass
from pathlib import Path

from ..shared.config_file import positive_int, read_config, section


@dataclass(frozen=True)
class ExplainConfig:
    max_diff_lines: int = 120
    max_files: int = 25


def load_explain_config(path: Path | None = None) -> ExplainConfig:
    """Read the `explain` section. A missing section means the defaults."""
    values = section(read_config(path), "explain")
    if not values:
        return ExplainConfig()
    return ExplainConfig(
        max_diff_lines=positive_int(values, "max_diff_lines", 120, "explain"),
        max_files=positive_int(values, "max_files", 25, "explain"),
    )
