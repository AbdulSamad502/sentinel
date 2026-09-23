"""Feeding real git commands to the Action Monitor.

The rules have understood dangerous git commands since Phase 1 - `push --force`,
`reset --hard`, `branch -D` are all in `sentinel.config.json` and all tested -
but nothing ever fed a real one in. A filesystem event cannot tell you that
`git push --force` was what ran, only that `.git` churned. So the demo scripted
its git actions, and `tasks.md` recorded the gap honestly.

This closes it. Installing the hooks writes small scripts into `.git/hooks/`
that report the command that invoked them, and the Action Monitor judges it
exactly as it judges a file write.

    python -m sentinel.action_monitor.githooks --install <repo>
    python -m sentinel.action_monitor.githooks --uninstall <repo>

**The hooks never block.** Every one exits 0 unconditionally, whatever Sentinel
makes of the command. A pre-push hook that exits non-zero aborts the push, and
Sentinel does not get to do that - it reports, and the human decides
(instructions.md #6). A BLOCKED verdict here means "this should not have
happened", exactly as it does everywhere else.

**What this can and cannot see.** Git only offers hooks for some operations, so
`push`, `commit` and `rebase` are covered. `reset --hard`, `clean -fd` and
`branch -D` have no hook at all and stay invisible - they remain in the config
as policy for the day another source can report them. Saying which is which
matters more than appearing to cover everything.
"""

import argparse
import sys
from pathlib import Path

from ..session import open_session
from ..shared.config_file import ConfigError, resolve_config_path
from .actions import Action, ActionType, Decision, Verdict
from .config import load_config
from .rules import check

# The hooks git actually gives us for the commands the config cares about.
HOOKS = ("pre-push", "pre-commit", "pre-rebase")

# What to call the command when the parent process cannot be read. Not a guess
# at the flags - just the operation, so it is recorded honestly rather than
# invented.
_FALLBACK = {"pre-push": "git push", "pre-commit": "git commit", "pre-rebase": "git rebase"}

_MARKER = "# installed by sentinel"

_TEMPLATE = """#!/bin/sh
{marker}
# Reports what git was asked to do. Never blocks: this script always exits 0.
# Remove with: python -m sentinel.action_monitor.githooks --uninstall "{root}"

# Drain stdin so git never blocks writing to a hook that does not read it.
cat >/dev/null 2>&1

# The hook's parent process is git itself, so its command line is the command
# the developer (or the coding agent) actually ran, flags included.
COMMAND=$(ps -o args= -p $PPID 2>/dev/null | tr -s ' ')
[ -z "$COMMAND" ] && COMMAND="{fallback}"

PYTHONPATH="{package_root}" "{python}" -m sentinel.action_monitor.githooks \\
    --record "$COMMAND" --repo "{root}" 2>/dev/null

exit 0
"""


def hook_script(hook: str, root: Path, python: str, package_root: Path) -> str:
    """The script installed for one hook."""
    return _TEMPLATE.format(
        marker=_MARKER,
        root=root,
        python=python,
        package_root=package_root,
        fallback=_FALLBACK[hook],
    )


def is_ours(path: Path) -> bool:
    """True if we wrote this hook, so uninstall never removes someone else's."""
    try:
        return _MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def install(root: Path, python: str | None = None, package_root: Path | None = None) -> tuple[list[str], list[str]]:
    """Write the hooks. Returns (installed, skipped-with-a-reason).

    An existing hook that we did not write is never overwritten - a project may
    already run linters or tests on commit, and silently replacing that would be
    a far worse thing to do than not installing.
    """
    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise ConfigError(f"{root} has no .git/hooks directory. Is it a git repository?")

    python = python or sys.executable
    package_root = package_root or Path(__file__).resolve().parent.parent.parent

    installed, skipped = [], []
    for hook in HOOKS:
        target = hooks_dir / hook
        if target.exists() and not is_ours(target):
            skipped.append(f"{hook} (a hook is already there and Sentinel did not write it)")
            continue
        try:
            target.write_text(hook_script(hook, root, python, package_root), encoding="utf-8")
            target.chmod(0o755)
            installed.append(hook)
        except OSError as exc:
            skipped.append(f"{hook} ({exc})")

    return installed, skipped


def uninstall(root: Path) -> list[str]:
    """Remove only the hooks Sentinel installed."""
    hooks_dir = root / ".git" / "hooks"
    removed = []
    for hook in HOOKS:
        target = hooks_dir / hook
        if target.is_file() and is_ours(target):
            try:
                target.unlink()
                removed.append(hook)
            except OSError:
                pass
    return removed


def normalise(command: str) -> str:
    """The git command as the rules want to see it.

    `ps` reports the full path the shell resolved, so a command arrives as
    `/opt/homebrew/bin/git push --force`. The config's patterns are written
    against `git push --force`, which is what a person would type.
    """
    tokens = command.split()
    for index, token in enumerate(tokens):
        # Split on both separators by hand rather than using os.path.basename,
        # which only knows the separator of the machine it is running on. This
        # string can come from a teammate's Windows box.
        name = token.replace("\\", "/").rsplit("/", 1)[-1]
        if name in ("git", "git.exe"):
            return " ".join(["git"] + tokens[index + 1 :])
    return " ".join(tokens)


def record_command(root: Path, command: str, config_path: Path | None = None) -> Decision:
    """Judge one git command and add it to the session. Never raises."""
    config_path = config_path or resolve_config_path(root)
    action = Action(type=ActionType.GIT, command=normalise(command))
    decision = check(action, load_config(config_path))

    log = open_session(root, config_path)
    if log.exists:
        log.record(action, decision)
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report real git commands to Sentinel's Action Monitor.")
    parser.add_argument("--install", metavar="REPO", help="write the hooks into a repo")
    parser.add_argument("--uninstall", metavar="REPO", help="remove the hooks Sentinel installed")
    parser.add_argument("--record", metavar="COMMAND", help="report one command (used by the hooks)")
    parser.add_argument("--repo", type=Path, default=Path("."), help="the repo, when recording")
    args = parser.parse_args(argv)

    if args.record:
        # Called from inside a git hook. It must never fail loudly, and must
        # never change git's exit status.
        try:
            decision = record_command(args.repo.resolve(), args.record)
        except (ConfigError, OSError):
            return 0
        if decision.verdict is not Verdict.ALLOWED:
            # stdout, not stderr: the hook sends stderr to /dev/null so a
            # Python traceback can never end up in the middle of a git push,
            # and a warning printed there would go the same way unseen.
            print(f"\n[sentinel] {decision.describe()}")
            print("[sentinel] Reporting only - this operation was not blocked.\n")
        return 0

    target = args.install or args.uninstall
    if not target:
        parser.error("give --install <repo> or --uninstall <repo>")

    root = Path(target).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    if args.uninstall:
        removed = uninstall(root)
        print(f"Removed {len(removed)} Sentinel hook(s): {', '.join(removed) or 'none'}")
        return 0

    try:
        installed, skipped = install(root)
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    print(f"Installed {len(installed)} hook(s) in {root}: {', '.join(installed) or 'none'}")
    for item in skipped:
        print(f"  skipped {item}")
    print("\nThese report git commands to Sentinel. They never block a git operation.")
    print("Covered: push, commit, rebase. Not covered: reset --hard, clean -fd,")
    print("branch -D - git offers no hook for those, so nothing can report them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
