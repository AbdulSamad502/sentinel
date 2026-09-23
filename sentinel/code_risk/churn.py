"""How often a file has changed lately, read from the local git history.

Churn is the Code Risk Analyzer's fourth signal: a file that half the recent
commits touch is unstable, and changing it is riskier than the diff alone
suggests. On a pull request the count comes from GitHub. On a local session it
has to come from the repo in front of us.

This is the only place in Sentinel that shells out to git, and it only ever
runs `git log`, which reads. Nothing here writes, stages, or checks anything
out (instructions.md #6).

Without it, every local verdict would carry a permanent "could not check the
churn history" caveat, and a caveat that appears on every clean change teaches
people to ignore all of them.
"""

import subprocess
from pathlib import Path

# A hung git call must never hang the verdict.
_TIMEOUT_SECONDS = 10


def is_git_repo(root: Path) -> bool:
    return (root / ".git").exists()


def commit_count_for_path(root: Path, relative_path: str, limit: int) -> int | None:
    """How many of the last `limit` commits touched this file, or None.

    None means git could not answer - not that the file is stable.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", "--oneline", f"-n{limit}", "--", relative_path],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # git missing, or it hung. Either way we do not know.
        return None

    if result.returncode != 0:
        return None
    return len([line for line in result.stdout.splitlines() if line.strip()])


def local_churn(
    root: Path, paths: list[str], lookback: int, file_limit: int = 10
) -> tuple[dict[str, int], list[str]]:
    """Churn counts for `paths`, plus anything that could not be read.

    Capped like the pull-request path is: this is a secondary signal, and a
    session touching eighty files should not pay eighty git calls for it.
    """
    if not paths:
        return {}, []
    if not is_git_repo(root):
        return {}, ["how often these files have changed recently (this folder is not a git repository)"]

    counts: dict[str, int] = {}
    for path in paths[:file_limit]:
        count = commit_count_for_path(root, path, lookback)
        if count is None:
            return counts, ["how often these files have changed recently (git could not be read)"]
        counts[path] = count

    unread = len(paths) - file_limit
    if unread > 0:
        return counts, [f"how often {unread} other file(s) have changed recently (checked the first {file_limit})"]
    return counts, []
