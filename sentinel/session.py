"""Recording a watching session so it can be explained afterwards.

The Action Monitor answers "was that allowed?" from paths alone. Explaining
*what the agent actually did* needs content, and content has to be captured
before the agent overwrites it: watchdog fires after the write has landed, so
by the time an event arrives the old bytes are already gone.

So the session snapshots the tree once, when watching starts, and records every
action against that baseline. A separate process can then read the result and
reconstruct the diff without the watcher still being alive — which is the real
workflow: the watcher runs in one terminal while the agent works, and you ask
"what did it just do?" from another.

    <watched-repo>/.sentinel/session/
        meta.json      when watching started, and what could not be captured
        actions.jsonl  one line per observed action
        before/        the tree as it was when watching started

`.sentinel/` is gitignored and already in IGNORED_DIRECTORIES, so Sentinel's
own writes never show up as agent activity.

Everything outside `<root>/.sentinel/` is opened read-only. Sentinel observes;
it does not touch the code it is watching (instructions.md #6).
"""

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .action_monitor.actions import Action, ActionType, Decision, Verdict
from .action_monitor.tree import is_safe_relative, walk_files
from .shared.config_file import positive_int, read_config, resolve_config_path, section, string_list

SESSION_DIRECTORY = Path(".sentinel") / "session"

# Read this much of a file to decide whether it is text. A NUL byte in the
# first block is the same test `git diff` uses, and it is good enough.
_BINARY_SNIFF_BYTES = 8000

_DEFAULT_SNAPSHOT_EXTENSIONS = frozenset(
    {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb", ".java", ".rs", ".php", ".cs", ".c", ".h",
        ".cpp", ".swift", ".kt", ".scala", ".sh", ".sql", ".html", ".css", ".scss", ".vue", ".svelte",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".md", ".rst", ".txt", ".tf",
        "Dockerfile", "Makefile", ".gitignore", ".dockerignore",
    }
)


@dataclass(frozen=True)
class SessionConfig:
    """Bounds on how much of the tree a session is willing to hold.

    A snapshot of every file in a large repo would be slow to take and useless
    to diff. Anything left out is named rather than dropped, so the verdict can
    say what it did not look at.
    """

    max_snapshot_bytes: int = 200_000
    max_total_bytes: int = 5_000_000
    snapshot_extensions: frozenset[str] = _DEFAULT_SNAPSHOT_EXTENSIONS


def load_session_config(path: Path | None = None) -> SessionConfig:
    """Read the `session` section. A missing section means the defaults."""
    values = section(read_config(path), "session")
    if not values:
        return SessionConfig()

    extensions = values.get("snapshot_extensions")
    if extensions is None:
        allowed = _DEFAULT_SNAPSHOT_EXTENSIONS
    else:
        allowed = frozenset(string_list(extensions, "session.snapshot_extensions"))

    return SessionConfig(
        max_snapshot_bytes=positive_int(values, "max_snapshot_bytes", 200_000, "session"),
        max_total_bytes=positive_int(values, "max_total_bytes", 5_000_000, "session"),
        snapshot_extensions=allowed,
    )


@dataclass(frozen=True)
class RecordedAction:
    """One line of `actions.jsonl`, read back."""

    path: str
    type: ActionType
    verdict: Verdict
    reasons: list[str]
    command: str = ""
    at: str = ""

    def as_decision(self) -> Decision:
        """Rebuild the Decision the watcher made, for the orchestrator."""
        return Decision(
            action=Action(type=self.type, path=self.path, command=self.command),
            verdict=self.verdict,
            reasons=list(self.reasons),
        )


class SessionLog:
    """The on-disk record of one watching session.

    Records; it does not interpret. Turning a baseline and a list of actions
    into diffs is `explainer/diff.py`'s job, which keeps this testable with no
    model and no network (instructions.md #5).
    """

    def __init__(self, root: Path, config: SessionConfig | None = None):
        self.root = root.resolve()
        self.config = config or SessionConfig()
        self.directory = self.root / SESSION_DIRECTORY
        self.before = self.directory / "before"
        self.actions_file = self.directory / "actions.jsonl"
        self.consultations_file = self.directory / "consultations.jsonl"
        self.meta_file = self.directory / "meta.json"
        # Problems worth telling the human about, rather than raising. A
        # session that cannot record everything is still worth having; it just
        # must not claim it saw everything (instructions.md #7).
        self.problems: list[str] = []
        self._skipped: list[dict] = []
        self._captured_bytes = 0

    # -- writing ---------------------------------------------------------

    def start(self) -> None:
        """Clear any previous session and snapshot the tree as it is now."""
        self._reset()
        for absolute, relative in walk_files(self.root):
            self._snapshot(absolute, relative)
        self._write_meta()

    def record(self, action: Action, decision: Decision) -> None:
        """Append one observed action. Never raises: the watcher must not die.

        Every action is recorded, including ALLOWED ones. The watcher's own
        `flagged` list deliberately keeps only the non-ALLOWED decisions, but
        you cannot explain work that was never written down.

        Git actions are recorded too, and carry a command instead of a path.
        They come from the git hooks rather than the watcher, since a
        filesystem event cannot tell you that `git push --force` was what ran.
        """
        if action.type is not ActionType.GIT and not action.path:
            return

        line = json.dumps(
            {
                "at": _now(),
                "type": action.type.value,
                "path": action.path,
                "command": action.command,
                "verdict": decision.verdict.value,
                "reasons": list(decision.reasons),
            }
        )
        try:
            with self.actions_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            self._note_problem(f"could not record an action to {self.actions_file.name} ({exc})")

    def record_consultation(self, tool: str, detail: str = "") -> None:
        """Note that the coding agent asked Sentinel something. Never raises.

        This is what keeps a self-correcting agent honest. If the agent can ask
        the supervisor and quietly fix what it finds, the human loses sight of
        everything that went wrong — and an agent optimising to pass Sentinel
        is not the same as an agent writing good code. So every consultation is
        written down and reported alongside the verdict.
        """
        if not self.exists:
            # Nobody is watching, so there is no session to attach this to.
            return
        try:
            with self.consultations_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": _now(), "tool": tool, "detail": detail}) + "\n")
        except OSError as exc:
            self._note_problem(f"could not record a consultation ({exc})")

    # -- reading ---------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.meta_file.is_file()

    def consultations(self) -> list[dict]:
        """Every time the coding agent asked Sentinel something, oldest first."""
        try:
            lines = self.consultations_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []

        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                entries.append(raw)
        return entries

    def recorded(self) -> list[RecordedAction]:
        """Every path the watcher touched, in the order it was first touched.

        A file saved eight times is one change to explain, not eight, so the
        last action for a path wins. Which kind of change it *was* is not read
        from here — `explainer/diff.py` derives that by comparing the baseline
        against the file on disk, which stays right even when the agent deleted
        a file and then recreated it.
        """
        latest: dict[str, RecordedAction] = {}
        for entry in self._read_actions():
            # Git actions have a command, not a path, and change no file on
            # their own. `decisions()` still reports them.
            if entry.type is ActionType.GIT or not entry.path:
                continue
            latest[entry.path] = entry
        return list(latest.values())

    def decisions(self) -> list[Decision]:
        """What the Action Monitor decided, reconciled against what is on disk.

        A delete for a file that exists again is dropped. Editors and tools save
        atomically - write a temp file, then rename over the target - and that
        produces a real DELETE event for a file nobody deleted. Reporting "the
        agent deleted src/app.py" while src/app.py sits there is the kind of
        false alarm that teaches people to ignore the tool.

        A genuine deletion leaves nothing on disk, so its decision survives. So
        does a delete-then-recreate, which nets out to a modification the
        explainer already describes correctly.
        """
        kept = []
        for entry in self._read_actions():
            if entry.type is ActionType.DELETE and entry.path and (self.root / entry.path).is_file():
                continue
            kept.append(entry.as_decision())
        return kept

    def before_text(self, relative_path: str) -> str | None:
        """The file's content when watching started, or None if it had none.

        None means the file did not exist at baseline — a genuine create. Use
        `was_skipped()` to tell that apart from "we chose not to capture it".
        """
        snapshot = self._snapshot_path(relative_path)
        if snapshot is None or not snapshot.is_file():
            return None
        try:
            return snapshot.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            self._note_problem(f"could not read the baseline copy of {relative_path} ({exc})")
            return None

    def current_text(self, relative_path: str) -> str | None:
        """The file's content now, or None if it is gone.

        A file that is present but too large or unreadable also returns None,
        and says so in `problems` — otherwise it would be indistinguishable
        from a delete, and Sentinel would report a change that never happened.
        """
        if not is_safe_relative(relative_path):
            return None
        current = self.root / relative_path
        if not current.is_file():
            return None

        try:
            if current.stat().st_size > self.config.max_snapshot_bytes:
                self._note_problem(
                    f"{relative_path} is larger than the {self.config.max_snapshot_bytes}-byte limit, "
                    "so its change could not be read"
                )
                return None
            content = current.read_bytes()
        except OSError as exc:
            self._note_problem(f"{relative_path} could not be read ({exc})")
            return None

        if b"\0" in content[:_BINARY_SNIFF_BYTES]:
            self._note_problem(f"{relative_path} looks like a binary file, so its change could not be read")
            return None
        return content.decode("utf-8", errors="replace")

    def recoverable(self, relative_path: str) -> Path | None:
        """Where this file's pre-session copy lives, if we still have one.

        A deleted file is not necessarily lost: the baseline snapshot took a
        copy before the agent touched anything. Sentinel does not restore it -
        it hands the human the path and lets them decide (instructions.md #6).
        """
        snapshot = self._snapshot_path(relative_path)
        return snapshot if snapshot is not None and snapshot.is_file() else None

    def skipped(self) -> list[dict]:
        """Files the baseline deliberately did not capture, with the reason."""
        meta = self._read_meta()
        entries = meta.get("skipped", [])
        return entries if isinstance(entries, list) else []

    def was_skipped(self, relative_path: str) -> str | None:
        """Why this path was left out of the baseline, or None if it wasn't."""
        for entry in self.skipped():
            if isinstance(entry, dict) and entry.get("path") == relative_path:
                return str(entry.get("why", "not captured"))
        return None

    def timeline(self, since: str = "") -> list[dict]:
        """Everything that happened, oldest first, actions and consultations together.

        Merged into one stream because "what happened while I was at lunch" is a
        question about the session, not about one log file. `since` is an ISO
        timestamp; entries at or after it are kept.
        """
        events: list[dict] = []

        for entry in self._read_actions():
            events.append(
                {
                    "at": entry.at,
                    "kind": "git" if entry.type is ActionType.GIT else "file",
                    "what": entry.command if entry.type is ActionType.GIT else f"{entry.type.value} {entry.path}",
                    "verdict": entry.verdict.value,
                    "reasons": entry.reasons,
                }
            )

        for entry in self.consultations():
            events.append(
                {
                    "at": str(entry.get("at", "")),
                    "kind": "consultation",
                    "what": f"the coding agent asked {entry.get('tool', 'something')}",
                    "verdict": "",
                    "reasons": [],
                }
            )

        events.sort(key=lambda event: event["at"])
        if since:
            events = [event for event in events if event["at"] >= since]
        return events

    def started_at(self) -> str:
        return str(self._read_meta().get("started", "an unknown time"))

    # -- internals -------------------------------------------------------

    def _reset(self) -> None:
        # Only ever removes the directory Sentinel itself created, under the
        # watched root. Nothing else in this repo deletes anything.
        if self.directory.exists() and self.directory.name == "session":
            shutil.rmtree(self.directory, ignore_errors=True)
        self.before.mkdir(parents=True, exist_ok=True)
        self.problems = []
        self._skipped = []
        self._captured_bytes = 0

    def _snapshot(self, absolute: Path, relative: str) -> None:
        if not self._wanted(relative):
            return

        try:
            size = absolute.stat().st_size
        except OSError as exc:
            self._skip(relative, f"could not be read ({exc})")
            return

        if size > self.config.max_snapshot_bytes:
            self._skip(relative, f"larger than the {self.config.max_snapshot_bytes}-byte snapshot limit")
            return
        if self._captured_bytes + size > self.config.max_total_bytes:
            self._skip(relative, "the session's total snapshot budget was already used up")
            return

        try:
            content = absolute.read_bytes()
        except OSError as exc:
            self._skip(relative, f"could not be read ({exc})")
            return

        if b"\0" in content[:_BINARY_SNIFF_BYTES]:
            self._skip(relative, "looks like a binary file")
            return

        destination = self._snapshot_path(relative)
        if destination is None:
            self._skip(relative, "has a path Sentinel will not write to")
            return

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        except OSError as exc:
            self._skip(relative, f"could not be copied ({exc})")
            return

        self._captured_bytes += size

    def _wanted(self, relative: str) -> bool:
        """Whether this file is the kind of thing we can usefully diff."""
        name = relative.rsplit("/", 1)[-1]
        if name in self.config.snapshot_extensions:
            return True
        suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        return suffix in self.config.snapshot_extensions

    def _snapshot_path(self, relative: str) -> Path | None:
        """Where a file's baseline copy lives, mirroring the tree's layout."""
        if not is_safe_relative(relative):
            return None
        return self.before.joinpath(*relative.split("/"))

    def _skip(self, relative: str, why: str) -> None:
        self._skipped.append({"path": relative, "why": why})

    def _note_problem(self, message: str) -> None:
        if message not in self.problems:
            self.problems.append(message)

    def _write_meta(self) -> None:
        meta = {
            "started": _now(),
            "root": str(self.root),
            "skipped": self._skipped,
        }
        try:
            self.meta_file.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            self._note_problem(f"could not write the session record ({exc})")

    def _read_meta(self) -> dict:
        try:
            raw = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _read_actions(self) -> list[RecordedAction]:
        try:
            lines = self.actions_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []

        entries = []
        for line in lines:
            entry = _parse_action(line)
            if entry is not None:
                entries.append(entry)
        return entries


def _parse_action(line: str) -> RecordedAction | None:
    """One JSONL line, or None if it is not one we can trust.

    A half-written final line is normal — the watcher may have been killed
    mid-append. Skipping it is better than failing the whole read.
    """
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
        return RecordedAction(
            path=str(raw.get("path", "")),
            type=ActionType(raw["type"]),
            verdict=Verdict(raw["verdict"]),
            reasons=[str(reason) for reason in raw.get("reasons", [])],
            command=str(raw.get("command", "")),
            at=str(raw.get("at", "")),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_session(root: Path, config_path: Path | None = None) -> SessionLog:
    """A SessionLog for `root`, configured from that repo's own config.

    Raises ConfigError with a message the user can act on if the config is
    unreadable — never a raw traceback.
    """
    return SessionLog(root=root, config=load_session_config(config_path or resolve_config_path(root)))
