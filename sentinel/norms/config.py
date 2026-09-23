"""Loading the `norms` section of a project's `sentinel.config.json`.

Norms are policy, so they are data like every other rule in this repo. Making
Sentinel stricter or looser for a project should never mean editing Python.

Errors name the norm and say what was expected: this file is written by a
human, often through the wizard but sometimes by hand, and a traceback is not
an error message (instructions.md #7).
"""

import re
from pathlib import Path

from ..action_monitor.actions import Verdict
from ..shared.config_file import ConfigError, read_config, section, string_list
from .norms import Norm

# ALLOWED would mean "a norm that is fine to break", which is not a norm.
_ALLOWED_SEVERITIES = {Verdict.FLAGGED, Verdict.BLOCKED}


def load_norms(path: Path | None = None) -> list[Norm]:
    """Read the project's norms. A missing section means no norms are set."""
    values = section(read_config(path), "norms")
    if not values:
        return []

    raw_rules = values.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("'norms.rules' must be a list of norm objects.")

    norms = [_to_norm(index, raw) for index, raw in enumerate(raw_rules)]

    seen: set[str] = set()
    for norm in norms:
        if norm.id in seen:
            raise ConfigError(f"Two norms share the id '{norm.id}'. Ids must be unique.")
        seen.add(norm.id)

    return norms


def _to_norm(index: int, raw: object) -> Norm:
    where = f"norms.rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object, got {type(raw).__name__}.")

    norm_id = raw.get("id")
    if not isinstance(norm_id, str) or not norm_id.strip():
        raise ConfigError(f"{where}.id must be a non-empty string.")

    statement = raw.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise ConfigError(f"norm '{norm_id}' needs a 'statement' saying, in plain English, what the rule is.")

    patterns = string_list(raw.get("patterns", []), f"norm '{norm_id}'.patterns")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"norm '{norm_id}' has a pattern that is not valid regex: {pattern!r} ({exc})") from exc

    return Norm(
        id=norm_id,
        statement=statement,
        severity=_to_severity(norm_id, raw.get("severity", Verdict.FLAGGED.value)),
        patterns=patterns,
        applies_to=string_list(raw.get("applies_to", []), f"norm '{norm_id}'.applies_to"),
        except_paths=string_list(raw.get("except_paths", []), f"norm '{norm_id}'.except_paths"),
    )


def _to_severity(norm_id: str, value: object) -> Verdict:
    try:
        severity = Verdict(str(value).upper())
    except ValueError:
        severity = None

    if severity not in _ALLOWED_SEVERITIES:
        allowed = " or ".join(sorted(v.value for v in _ALLOWED_SEVERITIES))
        raise ConfigError(f"norm '{norm_id}' has severity {value!r}. It must be {allowed}.")
    return severity
