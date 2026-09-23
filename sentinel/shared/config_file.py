"""Reading and validating `sentinel.config.json`.

Both the Action Monitor and the Code Risk Analyzer are configured from the same
file, so the file handling, the `_comment` convention, and the error messages
live here once rather than in each loader (instructions.md #2).

Each component still owns its own section and its own dataclass — this module
only deals with the file itself.
"""

import json
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "sentinel.config.json"

CONFIG_FILENAME = "sentinel.config.json"


class ConfigError(RuntimeError):
    """The config file is missing or malformed.

    Messages are written for the person who has to fix the file: name the key,
    say what was expected (instructions.md #7).
    """


def resolve_config_path(repo_root: Path | None = None) -> Path:
    """The config that governs `repo_root`, falling back to Sentinel's own.

    A watched project carries its own policy: point Sentinel at a folder and it
    reads that folder's `sentinel.config.json`, so different projects can hold
    their agent to different rules. Sentinel's own config is the fallback, which
    keeps every existing call site working unchanged.
    """
    if repo_root is not None:
        local = repo_root / CONFIG_FILENAME
        if local.is_file():
            return local
    return DEFAULT_CONFIG_PATH


def read_config(path: Path | None = None) -> dict:
    """Load the config file as a dict, or raise ConfigError explaining why not."""
    path = path or DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"No permission config at {path}. Copy the one from the repo root.") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object at the top level.")
    return raw


def section(raw: dict, name: str) -> dict:
    """A named object from the config, with its `_comment` keys removed.

    The config documents itself with `_comment` entries; they must never be
    mistaken for rules.
    """
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' in the config must be an object, got {type(value).__name__}.")
    return {key: item for key, item in value.items() if not key.startswith("_")}


def string_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} must be a list of strings.")
    return list(value)


def positive_int(values: dict, name: str, fallback: int, where: str) -> int:
    value = values.get(name, fallback)
    # bool is an int subclass, and `true` in a threshold field is a mistake.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where}.{name} must be a positive whole number, got {value!r}.")
    return value
