"""Reconstructing what changed, from a recorded session.

Pure and offline: it takes a `SessionLog` and compares the baseline it captured
against the files on disk now. No model, no network, no git (instructions.md #4
— stdlib `difflib` does this job, so nothing new is installed for it).

`summarise()` is the load-bearing piece. It answers "what did the agent do?"
with no model involved at all, so Sentinel still has a real answer when Ollama
is down or Bedrock is unreachable. The narration in `explain.py` is an
improvement on this text, never a replacement for it.
"""

import difflib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from ..session import SessionLog

ADDED = "added"
MODIFIED = "modified"
DELETED = "deleted"

# Widest status word, so the summary lines up in a terminal.
_STATUS_WIDTH = len(MODIFIED)


@dataclass(frozen=True)
class FileChange:
    """One file's before and after, and the diff between them."""

    path: str
    status: str
    before: str = ""
    after: str = ""
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    diff: str = ""

    def describe(self) -> str:
        """One line, ASCII only - this goes to a terminal during the demo."""
        counts = []
        if self.added_lines:
            counts.append(f"+{len(self.added_lines)}")
        if self.removed_lines:
            counts.append(f"-{len(self.removed_lines)}")
        tail = f" ({' '.join(counts)})" if counts else ""
        return f"{self.status.ljust(_STATUS_WIDTH)} {self.path}{tail}"


def build_changes(log: SessionLog) -> tuple[list[FileChange], list[str]]:
    """Every net change in the session, plus what could not be reconstructed.

    Returns `(changes, unchecked)`. A path Sentinel could not read lands in
    `unchecked` rather than being dropped: a change we could not look at must
    never be indistinguishable from one that did not happen.
    """
    changes: list[FileChange] = []
    unchecked: list[str] = []

    for entry in log.recorded():
        why_skipped = log.was_skipped(entry.path)
        if why_skipped is not None:
            unchecked.append(f"what changed in {entry.path} ({why_skipped})")
            continue

        before = log.before_text(entry.path)
        after = log.current_text(entry.path)

        if before is None and after is None:
            # Created and removed again inside the session, or unreadable at
            # both ends. Either way there is no net change to describe; if it
            # was unreadable, `log.problems` already says so.
            continue
        if before == after:
            # Touched but not actually changed - a formatter rewriting
            # identical bytes, or an edit the agent reverted.
            continue

        changes.append(to_change(entry.path, before, after))

    # Read after the loop: current_text() records its problems as it goes.
    unchecked.extend(log.problems)
    return changes, unchecked


def to_change(path: str, before: str | None, after: str | None) -> FileChange:
    """One file's change, from its before and after text. `None` means absent."""
    if before is None:
        status = ADDED
    elif after is None:
        status = DELETED
    else:
        status = MODIFIED

    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()

    diff_lines = list(
        difflib.unified_diff(before_lines, after_lines, fromfile=f"before/{path}", tofile=f"after/{path}", lineterm="")
    )
    # `---`/`+++` are the file headers, not content. Counting them as changed
    # lines would inflate every file by two.
    body = [line for line in diff_lines if not line.startswith(("---", "+++"))]

    return FileChange(
        path=path,
        status=status,
        before=before or "",
        after=after or "",
        added_lines=[line[1:] for line in body if line.startswith("+")],
        removed_lines=[line[1:] for line in body if line.startswith("-")],
        diff="\n".join(diff_lines),
    )


def summarise(changes: list[FileChange], unchecked: list[str] | None = None) -> str:
    """The change as a human reads it, with no model involved."""
    unchecked = unchecked or []

    if not changes:
        lines = ["No file changes recorded in this session."]
    else:
        added = sum(len(change.added_lines) for change in changes)
        removed = sum(len(change.removed_lines) for change in changes)
        lines = [f"{len(changes)} file(s) changed, +{added} -{removed}", ""]
        lines += [f"  {change.describe()}" for change in changes]

    if unchecked:
        lines.append("")
        lines.append("Could not read:")
        lines += [f"  - {item}" for item in unchecked]

    return "\n".join(lines)


@dataclass(frozen=True)
class Recovery:
    """A deleted file that can still be got back, and how."""

    path: str
    snapshot: Path
    destination: Path

    def command(self) -> str:
        """The exact shell command to restore it. ASCII only.

        Both sides are absolute so the command works from any directory. A
        relative destination would silently restore into whatever folder the
        terminal happened to be in.
        """
        return f'cp "{self.snapshot}" "{self.destination}"'


def recoveries(log: SessionLog, changes: list[FileChange]) -> list[Recovery]:
    """Deleted files whose pre-session copy Sentinel still holds.

    The baseline snapshot took a copy of every file before the agent touched
    anything, so a deletion is usually not final. Saying so turns the session
    log into a safety net without Sentinel ever acting: it reports the path and
    the command, and the human runs it (instructions.md #6).
    """
    found = []
    for change in changes:
        if change.status != DELETED:
            continue
        snapshot = log.recoverable(change.path)
        if snapshot is not None:
            found.append(Recovery(path=change.path, snapshot=snapshot, destination=log.root / change.path))
    return found


def recovery_report(items: list[Recovery]) -> str:
    """What to tell the human about files they can still get back."""
    if not items:
        return ""

    lines = [f"{len(items)} deleted file(s) can be restored from the session snapshot:", ""]
    for item in items:
        lines.append(f"  {item.path}")
        lines.append(f"    {item.command()}")
    lines.append("")
    lines.append("Sentinel will not run these for you.")
    return "\n".join(lines)


def numbered_additions(change: FileChange) -> list[tuple[int, str]]:
    """Every added line as (line number in the new file, text).

    Read out of the diff's hunk headers rather than by searching the new file
    for the text: a line that already appeared elsewhere would otherwise be
    reported at the wrong place, and a norm violation pointing at the wrong
    line is worse than no line number at all.
    """
    additions: list[tuple[int, str]] = []
    line_number = 0

    for line in change.diff.splitlines():
        if line.startswith("@@"):
            line_number = _hunk_start(line)
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            additions.append((line_number, line[1:]))
            line_number += 1
        elif line.startswith("-"):
            continue
        else:
            # Context line: present in both sides, so it advances the new file.
            line_number += 1

    return additions


def _hunk_start(header: str) -> int:
    """The new-file line number a `@@ -a,b +c,d @@` hunk starts at."""
    for part in header.split():
        if part.startswith("+"):
            digits = part[1:].split(",")[0]
            if digits.isdigit():
                return int(digits)
    return 1


def truncate_diff(diff: str, max_lines: int) -> str:
    """A diff cut to `max_lines`, saying so when it was cut.

    The marker is not decoration. A model handed a silently truncated diff will
    describe a partial change as if it were the whole one.
    """
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    hidden = len(lines) - max_lines
    return "\n".join(lines[:max_lines] + [f"... {hidden} more diff line(s) not shown"])


# Paths whose contents are never couriered to a model provider. Hardcoded rather
# than read from the watched repo's `protected_paths`: that config is a verdict
# policy, it can be empty, and "your secrets were not sent anywhere" cannot be
# opt-in. Matched against the whole path and against the basename.
_SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "*secrets*",
    "*credentials*",
)

WITHHELD = "(diff withheld: secret-bearing path)"


def is_secret_path(path: str) -> bool:
    """Whether this path's contents are withheld from model prompts."""
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(path, p) or fnmatchcase(name, p) for p in _SECRET_PATTERNS)


def diff_body(change: FileChange, max_lines: int) -> str:
    """The diff as a model is shown it, or a marker in place of a secret's values.

    The model still learns the file changed and how - only the old and new values
    are held back. The verdict does not read this: `protected_paths` decides what
    touching `.env` means, deterministically, from the change record itself.
    """
    if is_secret_path(change.path):
        return WITHHELD
    return truncate_diff(change.diff, max_lines)


def diff_block(change: FileChange, max_lines: int) -> str:
    """One file's heading and diff, as every prompt builder shows it."""
    return f"### {change.path} ({change.status})\n{diff_body(change, max_lines)}"
