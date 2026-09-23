"""Walking a working tree the way Sentinel looks at it.

Which directories are worth looking at is a judgement two modules now share —
the watcher (don't raise an event) and the session log (don't snapshot it) — so
it lives here once rather than in each (instructions.md #2).

This is *not* the config's deny list. Deny is policy: "the agent should not be
writing here". This is signal-to-noise: "don't even look". `.git` is in both,
for different reasons. Keep them separate.
"""

import os
from collections.abc import Iterator
from pathlib import Path

from .rules import normalise_path

# A coding agent running `git commit` rewrites .git constantly and a test run
# fills __pycache__. Reporting those would bury the events that matter.
IGNORED_DIRECTORIES = {".git", ".venv", "venv", "__pycache__", "node_modules", ".sentinel", ".pytest_cache"}


def is_noise(relative_path: str) -> bool:
    """True for paths inside directories we deliberately don't report on."""
    return any(part in IGNORED_DIRECTORIES for part in Path(relative_path).parts)


def is_safe_relative(relative_path: str) -> bool:
    """True if the path stays inside the tree it is relative to.

    Guards every place a repo-relative path is turned back into a filesystem
    path. A path that climbs out with `..`, or one that is absolute, would let
    a snapshot be written outside the directory Sentinel owns.
    """
    if not relative_path or relative_path.startswith("/"):
        return False
    parts = relative_path.split("/")
    return ".." not in parts and ":" not in parts[0]


def walk_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Every file under `root` worth looking at, as (absolute, repo-relative).

    Prunes ignored directories in place rather than filtering afterwards, so a
    `node_modules` with 40,000 files in it is never descended into at all.
    Symlinks are skipped: following one can leave the tree entirely.
    """
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d not in IGNORED_DIRECTORIES)
        for filename in sorted(filenames):
            absolute = Path(directory) / filename
            if absolute.is_symlink() or not absolute.is_file():
                continue
            try:
                relative = normalise_path(str(absolute.relative_to(root)))
            except ValueError:
                continue
            if relative and not is_noise(relative):
                yield absolute, relative
