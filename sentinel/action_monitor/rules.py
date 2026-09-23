"""The check: given an action and the permission config, is it OK?

`check()` is a pure function — no file reads, no clock, no network — so it can
be exercised against mock actions on its own (instructions.md #5). Everything
it knows comes from the `MonitorConfig` handed to it.

Sentinel only ever reports. A BLOCKED verdict here means "this should not have
happened", not "Sentinel stopped it".
"""

import posixpath
from fnmatch import fnmatchcase

from .actions import Action, ActionType, Decision, Verdict, worst
from .config import MonitorConfig


def normalise_path(path: str) -> str:
    """Repo-relative, forward slashes, no './' prefix.

    Windows and macOS teammates must get identical verdicts from identical
    config, so the separator is normalised before any pattern is matched.
    """
    cleaned = str(path).replace("\\", "/").strip()
    if not cleaned:
        return ""
    # normpath, not lstrip('./') — lstrip strips a *character set*, which would
    # turn '.env' into 'env' and quietly dodge every dotfile rule below.
    normalised = posixpath.normpath(cleaned)
    return "" if normalised == "." else normalised


def matches(path: str, pattern: str) -> bool:
    """Glob match against a normalised path.

    `fnmatchcase` is used rather than `fnmatch` because `fnmatch` folds case on
    Windows only, which would silently give the team different results on
    different laptops. Note that `*` crosses '/', so 'src/*' also matches
    'src/deep/file.py' — this is intentional and documented in the config.
    """
    pattern = pattern.replace("\\", "/")
    if fnmatchcase(path, pattern):
        return True
    # A bare directory name in the config should cover everything beneath it.
    return fnmatchcase(path, f"{pattern.rstrip('/')}/*")


def _path_findings(action: Action, config: MonitorConfig) -> list[tuple[Verdict, str]]:
    """Every path-based rule that fires for this action."""
    path = normalise_path(action.path)
    findings: list[tuple[Verdict, str]] = []

    for pattern in config.deny:
        if matches(path, pattern):
            findings.append((Verdict.BLOCKED, f"'{path}' is in a denied location (matches '{pattern}')"))

    # An empty allow list means "anywhere that isn't denied", so only enforce
    # this when the team has actually scoped the agent to specific folders.
    if config.allow and not any(matches(path, pattern) for pattern in config.allow):
        findings.append((Verdict.FLAGGED, f"'{path}' is outside the folders this agent was scoped to"))

    for pattern, verdict in config.protected_paths.items():
        if matches(path, pattern):
            findings.append((verdict, f"'{path}' is a protected file (matches '{pattern}')"))

    for pattern in config.dependency_patterns:
        if matches(path, pattern):
            findings.append(
                (config.dependency_verdict, f"'{path}' changes project dependencies - needs a human to confirm")
            )
            break

    return findings


def _git_findings(action: Action, config: MonitorConfig) -> list[tuple[Verdict, str]]:
    """Every dangerous-command rule that fires for a git action."""
    command = " ".join(action.command.lower().split())
    return [
        (verdict, f"git command contains '{pattern}', which can destroy work that isn't recoverable")
        for pattern, verdict in config.git_commands.items()
        if pattern in command
    ]


def check(action: Action, config: MonitorConfig) -> Decision:
    """Judge one action. The most severe finding decides; all are reported."""
    if action.type is ActionType.GIT:
        findings = _git_findings(action, config)
    else:
        findings = _path_findings(action, config)

    baseline = config.verdict_for(action.type)
    reasons = [reason for _, reason in findings]

    if baseline is not Verdict.ALLOWED:
        reasons.append(f"'{action.type.value}' actions are set to {baseline.value} in this repo's config")

    verdict = worst([verdict for verdict, _ in findings] + [baseline])
    return Decision(action=action, verdict=verdict, reasons=reasons if verdict is not Verdict.ALLOWED else [])


def check_all(actions: list[Action], config: MonitorConfig) -> list[Decision]:
    """Judge a batch of actions, worst first so the reader sees the problem."""
    decisions = [check(action, config) for action in actions]
    ranking = {Verdict.BLOCKED: 0, Verdict.FLAGGED: 1, Verdict.ALLOWED: 2}
    return sorted(decisions, key=lambda decision: ranking[decision.verdict])
