"""The repos this person has put under supervision.

Until now Sentinel had two kinds of state, both scoped to a place: per-repo
(`<watched-repo>/.sentinel/session/`) and per-install
(`<sentinel>/.sentinel/model`). Running several agents from one dashboard needs
a third: a per-user list of *which* repos are supervised at all.

    ~/.sentinel/agents.json

That is the whole of it - a list of folders and the last process id we started
for each. No policy lives here; each repo's rules stay in that repo's own
`sentinel.config.json`, which is what makes a supervised project portable.

This module is data and file IO only. Starting and stopping the processes is
`supervisor.py`'s job, and keeping the two apart is what lets this be tested
without spawning anything.
"""

import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

# Overridable so tests never touch the developer's real registry, and so a
# future desktop build can put this inside its own application-support folder.
_HOME_VARIABLE = "SENTINEL_HOME"

REGISTRY_FILENAME = "agents.json"
REGISTRY_VERSION = 1


class RegistryError(RuntimeError):
    """The registry could not be read or a folder could not be supervised.

    Carries a message the user can act on directly (instructions.md #7).
    """


def home() -> Path:
    """Where this user's Sentinel state lives."""
    override = os.environ.get(_HOME_VARIABLE, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".sentinel"


def registry_path() -> Path:
    return home() / REGISTRY_FILENAME


@dataclass(frozen=True)
class Agent:
    """One supervised repo.

    `pid` is the last process id we started for it, or 0. It is a *hint* - the
    process may have exited, and a pid can be reused by something else
    entirely. `supervisor.status()` is what actually decides whether an agent
    is running; nothing here should be trusted as liveness.
    """

    id: str
    name: str
    root: Path
    added: str
    pid: int = 0

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "root": str(self.root),
            "added": self.added,
            "pid": self.pid,
        }


def _from_json(raw: object) -> Agent | None:
    """One entry, or None if it is unusable.

    A malformed entry is dropped rather than raised on. The registry is a
    convenience list; one bad row must not lock a person out of every other
    agent they have.
    """
    if not isinstance(raw, dict):
        return None
    identifier, root = raw.get("id"), raw.get("root")
    if not isinstance(identifier, str) or not identifier or not isinstance(root, str) or not root:
        return None

    pid = raw.get("pid", 0)
    return Agent(
        id=identifier,
        name=str(raw.get("name") or Path(root).name),
        root=Path(root),
        added=str(raw.get("added", "")),
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else 0,
    )


def load() -> list[Agent]:
    """Every supervised repo. A missing registry means none yet, not an error."""
    path = registry_path()
    if not path.is_file():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"{path} is not valid JSON: {exc}\nFix it, or delete it to start a fresh list."
        ) from exc
    except OSError as exc:
        raise RegistryError(f"Could not read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError(f"{path} must contain a JSON object at the top level.")

    entries = raw.get("agents", [])
    if not isinstance(entries, list):
        raise RegistryError(f"'agents' in {path} must be a list.")

    return [agent for agent in (_from_json(entry) for entry in entries) if agent is not None]


def save(agents: list[Agent]) -> None:
    """Replace the registry with this list."""
    path = registry_path()
    document = {"version": REGISTRY_VERSION, "agents": [agent.as_json() for agent in agents]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"Could not write {path}: {exc}") from exc


def get(agent_id: str) -> Agent | None:
    return next((agent for agent in load() if agent.id == agent_id), None)


def resolve_root(folder: str | Path) -> Path:
    """The folder to supervise, or a refusal explaining why not.

    Every path entering Sentinel from the network goes through here. It must
    resolve symlinks, because the dashboard hands this whatever a user typed
    and a supervised folder is somewhere Sentinel will later write a config.
    """
    text = str(folder).strip()
    if not text:
        raise RegistryError("No folder given.")

    try:
        root = Path(text).expanduser().resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop
        raise RegistryError(f"That path cannot be resolved: {exc}") from exc

    if not root.exists():
        raise RegistryError(f"There is nothing at {root}.")
    if not root.is_dir():
        raise RegistryError(f"{root} is a file, not a folder. Point Sentinel at a project folder.")
    return root


def add(folder: str | Path, name: str = "") -> Agent:
    """Put a folder under supervision. Adding one twice returns the first."""
    root = resolve_root(folder)
    agents = load()

    existing = next((agent for agent in agents if agent.root == root), None)
    if existing is not None:
        return existing

    agent = Agent(
        id=uuid.uuid4().hex[:12],
        name=(name.strip() or root.name),
        root=root,
        added=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    save(agents + [agent])
    return agent


def remove(agent_id: str) -> bool:
    """Stop listing a repo. Deletes nothing inside it - not the code, not the
    config, not the recorded session. Removing an agent from a list is not a
    reason to destroy someone's work (instructions.md #6)."""
    agents = load()
    remaining = [agent for agent in agents if agent.id != agent_id]
    if len(remaining) == len(agents):
        return False
    save(remaining)
    return True


def set_pid(agent_id: str, pid: int) -> None:
    """Remember the process we last started for this agent."""
    agents = load()
    updated = [replace(agent, pid=max(pid, 0)) if agent.id == agent_id else agent for agent in agents]
    save(updated)
