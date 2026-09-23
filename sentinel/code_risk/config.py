"""Loads the Code Risk Analyzer's section of `sentinel.config.json`.

Same file and same error handling as the Action Monitor's loader — the two
components share one config file so there is one place to tune Sentinel.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..shared.config_file import ConfigError, positive_int, read_config, section, string_list

__all__ = ["ConfigError", "RiskConfig", "load_risk_config"]


@dataclass(frozen=True)
class RiskConfig:
    """Thresholds and keyword lists for the risk heuristics."""

    sensitive_paths: dict[str, list[str]] = field(default_factory=dict)
    test_patterns: list[str] = field(default_factory=list)
    source_extensions: set[str] = field(default_factory=set)
    ignore_extensions: set[str] = field(default_factory=set)
    large_file_changes: int = 300
    huge_file_changes: int = 800
    large_change_total: int = 1000
    many_files: int = 30
    high_churn_commits: int = 10
    churn_lookback: int = 30


def load_risk_config(path: Path | None = None) -> RiskConfig:
    """Read the `code_risk` section. A missing section means the defaults."""
    risk = section(read_config(path), "code_risk")
    defaults = RiskConfig()
    thresholds = section(risk, "thresholds")
    where = "code_risk.thresholds"

    sensitive = {
        area: string_list(patterns, f"code_risk.sensitive_paths['{area}']")
        for area, patterns in section(risk, "sensitive_paths").items()
    }

    return RiskConfig(
        sensitive_paths=sensitive,
        test_patterns=string_list(
            section(risk, "test_paths").get("patterns", []), "code_risk.test_paths.patterns"
        ),
        source_extensions=set(
            string_list(
                section(risk, "source_extensions").get("extensions", []),
                "code_risk.source_extensions.extensions",
            )
        ),
        ignore_extensions=set(
            string_list(
                section(risk, "ignore_extensions").get("extensions", []),
                "code_risk.ignore_extensions.extensions",
            )
        ),
        large_file_changes=positive_int(thresholds, "large_file_changes", defaults.large_file_changes, where),
        huge_file_changes=positive_int(thresholds, "huge_file_changes", defaults.huge_file_changes, where),
        large_change_total=positive_int(thresholds, "large_change_total", defaults.large_change_total, where),
        many_files=positive_int(thresholds, "many_files", defaults.many_files, where),
        high_churn_commits=positive_int(thresholds, "high_churn_commits", defaults.high_churn_commits, where),
        churn_lookback=positive_int(thresholds, "churn_lookback", defaults.churn_lookback, where),
    )
