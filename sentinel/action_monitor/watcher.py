"""Watches a working tree and reports what the coding agent is doing to it.

This is the Action Monitor's eyes. It turns real filesystem activity into
`Action` objects, runs each one through `rules.check()`, and prints the verdict
as it happens.

It also records the session to `<repo>/.sentinel/session/`, snapshotting the
tree before it starts listening. That snapshot is what lets the Change
Explainer answer "what did it do?" afterwards: a filesystem event arrives
*after* the write has landed, so waiting for one means the original bytes are
already gone. Pass `--no-record` to skip it, at the cost of that answer.

Sentinel observes and reports. Nothing here reverts, deletes, or blocks
anything — a BLOCKED verdict means "this should not have happened", and acting
on it is the human's call (instructions.md #6). The only thing written outside
Sentinel's own repo is that session directory.

    python -m sentinel.action_monitor.watcher <path-to-watch>
"""

import argparse
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..session import SessionLog, open_session
from ..shared.config_file import resolve_config_path
from .actions import Action, ActionType, Decision, Verdict
from .config import ConfigError, MonitorConfig, load_config
from .rules import check, normalise_path
from .tree import is_noise

# Editors and formatters fire several events for one logical save. Collapsing
# repeats of the same action within this window keeps the output readable.
DEBOUNCE_SECONDS = 1.0

_EVENT_TYPES = {
    "created": ActionType.CREATE,
    "modified": ActionType.MODIFY,
    "deleted": ActionType.DELETE,
    "moved": ActionType.CREATE,
}

_VERDICT_MARKERS = {
    Verdict.ALLOWED: "ok  ",
    Verdict.FLAGGED: "FLAG",
    Verdict.BLOCKED: "STOP",
}


def format_decision(decision: Decision) -> str:
    marker = _VERDICT_MARKERS[decision.verdict]
    line = f"[{marker}] {decision.action.describe()}"
    if not decision.reasons:
        return line
    return line + "".join(f"\n         - {reason}" for reason in decision.reasons)


class ActionReporter(FileSystemEventHandler):
    """Bridges watchdog events into Sentinel actions and prints the verdicts.

    Keeps every non-ALLOWED decision in `flagged` so the orchestrator can ask
    "what has this agent done so far?" in Phase 3.

    With a `session`, it also writes every decision — ALLOWED ones included —
    to disk, so a second process can explain the change afterwards. The two
    records answer different questions and are deliberately not merged:
    `flagged` is "what went wrong", the session is "what happened".
    """

    def __init__(self, root: Path, config: MonitorConfig, session: SessionLog | None = None):
        self.root = root
        self.config = config
        self.session = session
        self.flagged: list[Decision] = []
        self._recent: dict[str, float] = {}

    def to_action(self, event: FileSystemEvent) -> Action | None:
        """Convert a watchdog event, or None if it isn't worth reporting."""
        if event.is_directory:
            return None

        action_type = _EVENT_TYPES.get(event.event_type)
        if action_type is None:
            return None

        # For a move, the destination is what the agent actually produced.
        raw_path = getattr(event, "dest_path", "") or event.src_path
        try:
            relative = Path(raw_path).resolve().relative_to(self.root)
        except ValueError:
            # Outside the watched tree — not ours to judge.
            return None

        path = normalise_path(str(relative))
        if not path or is_noise(path):
            return None
        return Action(type=action_type, path=path)

    def _is_repeat(self, action: Action) -> bool:
        """True if this is the same edit we just reported.

        Debounced per path rather than per (path, type), because one `echo >
        file` produces a create *and* a modify and reporting both is noise.
        A delete is never suppressed — losing a file is exactly the event the
        human needs to see, even moments after the file was written.
        """
        now = time.monotonic()
        last_seen = self._recent.get(action.path)
        self._recent[action.path] = now

        if action.type is ActionType.DELETE:
            return False
        return last_seen is not None and now - last_seen < DEBOUNCE_SECONDS

    def on_any_event(self, event: FileSystemEvent) -> None:
        action = self.to_action(event)
        if action is None or self._is_repeat(action):
            return

        decision = check(action, self.config)
        if decision.verdict is not Verdict.ALLOWED:
            self.flagged.append(decision)
        if self.session is not None:
            self.session.record(action, decision)
        print(format_decision(decision), flush=True)

    def summarise(self) -> str:
        if not self.flagged:
            return "Nothing flagged."

        blocked = sum(1 for d in self.flagged if d.verdict is Verdict.BLOCKED)
        flagged = sum(1 for d in self.flagged if d.verdict is Verdict.FLAGGED)
        lines = [f"{blocked} blocked, {flagged} flagged:"]
        lines += [f"  {decision.describe()}" for decision in self.flagged]
        return "\n".join(lines)


def watch(root: Path, config: MonitorConfig, session: SessionLog | None = None) -> ActionReporter:
    """Watch `root` until interrupted, reporting every action. Blocking."""
    reporter = ActionReporter(root=root, config=config, session=session)

    if session is not None:
        # Snapshot before the observer starts. A file the agent overwrites in
        # the first second still has a "before" this way; taken any later, the
        # original bytes are already gone.
        print("Taking a snapshot of the tree so this session can be explained later...")
        session.start()
        for problem in session.problems:
            print(f"  note: {problem}")

    observer = Observer()
    observer.schedule(reporter, str(root), recursive=True)
    observer.start()

    print(f"Sentinel is watching {root}")
    print("Reporting only - nothing here blocks or reverts anything. Ctrl-C to stop.\n")
    try:
        while observer.is_alive():
            observer.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()

    print("\n" + reporter.summarise())
    if session is not None:
        print(f"\nSession recorded. Ask what changed with:\n  python -m sentinel.explainer.explain {root}")
    return reporter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch a working tree and report what a coding agent does to it.")
    parser.add_argument("path", nargs="?", default=".", help="directory to watch (default: current)")
    parser.add_argument("--config", type=Path, default=None, help="path to sentinel.config.json")
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="do not snapshot the tree; the session cannot be explained afterwards",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    # A watched project carries its own policy, so a config sitting in that
    # repo wins over Sentinel's own.
    config_path = args.config or resolve_config_path(root)
    try:
        config = load_config(config_path)
        session = None if args.no_record else open_session(root, config_path)
    except ConfigError as exc:
        print(f"\nCannot start: {exc}\n", file=sys.stderr)
        return 1

    watch(root, config, session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
