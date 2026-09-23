"""Loads the permission policy that tells the Action Monitor what's allowed.

All the policy lives in `sentinel.config.json` as data. This module only reads
its sections and validates them — no rules are hardcoded here, so tuning
Sentinel's strictness never means editing Python.

File handling is shared with the Code Risk Analyzer's loader; see
`sentinel/shared/config_file.py`.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..shared.config_file import ConfigError, read_config, section, string_list
from .actions import ActionType, Verdict

# Re-exported so callers can `from ...config import ConfigError` next to the
# loader they are already using.
__all__ = ["ConfigError", "MonitorConfig", "load_config"]


@dataclass(frozen=True)
class MonitorConfig:
    """The loaded, validated permission policy."""

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    protected_paths: dict[str, Verdict] = field(default_factory=dict)
    dependency_patterns: list[str] = field(default_factory=list)
    dependency_verdict: Verdict = Verdict.FLAGGED
    action_verdicts: dict[ActionType, Verdict] = field(default_factory=dict)
    git_commands: dict[str, Verdict] = field(default_factory=dict)

    def verdict_for(self, action_type: ActionType) -> Verdict:
        """Baseline verdict for an action type, before path rules apply."""
        return self.action_verdicts.get(action_type, Verdict.ALLOWED)


def _to_verdict(value: object, where: str) -> Verdict:
    try:
        return Verdict(str(value).upper())
    except ValueError as exc:
        valid = ", ".join(v.value for v in Verdict)
        raise ConfigError(f"{where}: '{value}' is not a verdict. Use one of: {valid}.") from exc


def load_config(path: Path | None = None) -> MonitorConfig:
    """Read the permission policy from disk."""
    raw = read_config(path)

    paths = section(raw, "paths")
    dependencies = section(raw, "dependency_files")

    protected = {
        pattern: _to_verdict(value, f"protected_paths['{pattern}']")
        for pattern, value in section(raw, "protected_paths").items()
    }

    action_verdicts = {}
    for name, value in section(raw, "action_verdicts").items():
        try:
            action_type = ActionType(name)
        except ValueError as exc:
            valid = ", ".join(t.value for t in ActionType)
            raise ConfigError(f"action_verdicts: '{name}' is not an action type. Use: {valid}.") from exc
        action_verdicts[action_type] = _to_verdict(value, f"action_verdicts['{name}']")

    git_commands = {}
    for verdict_name, patterns in section(raw, "git_commands").items():
        verdict = _to_verdict(verdict_name, "git_commands")
        for pattern in string_list(patterns, f"git_commands['{verdict_name}']"):
            git_commands[pattern.lower()] = verdict

    return MonitorConfig(
        allow=string_list(paths.get("allow", []), "paths.allow"),
        deny=string_list(paths.get("deny", []), "paths.deny"),
        protected_paths=protected,
        dependency_patterns=string_list(dependencies.get("patterns", []), "dependency_files.patterns"),
        dependency_verdict=_to_verdict(dependencies.get("verdict", "FLAGGED"), "dependency_files.verdict"),
        action_verdicts=action_verdicts,
        git_commands=git_commands,
    )
