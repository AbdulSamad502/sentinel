"""Scores a set of changed files for risk, and says why.

Four heuristics, all cheap and all explainable (instructions.md #3):

  1. does this touch a sensitive area (payments, auth, permissions, ...)
  2. is the file churning — changed unusually often lately
  3. does it change source code without touching any test
  4. is the diff unusually large

Every threshold and keyword lives in `sentinel.config.json`. Nothing here is
tuned by editing Python.

The churn signal needs GitHub; the other three do not. When GitHub is
unavailable the analysis still returns, saying churn could not be checked,
rather than failing (instructions.md #7).
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath

from ..action_monitor.rules import matches, normalise_path
from ..shared.github import ChangedFile, GitHubClient, GitHubError
from .config import RiskConfig


class RiskLevel(str, Enum):
    """How much attention a change deserves.

    Separate from the Action Monitor's Verdict: that answers "was this action
    permitted", this answers "how likely is this change to hurt". The
    orchestrator combines them in Phase 3.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_SEVERITY = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def highest(levels: list[RiskLevel]) -> RiskLevel:
    return max(levels, key=lambda level: _SEVERITY[level], default=RiskLevel.LOW)


@dataclass(frozen=True)
class FileRisk:
    """The risk verdict for one changed file."""

    path: str
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.reasons:
            return f"{self.level.value}: {self.path}"
        return f"{self.level.value}: {self.path} - " + "; ".join(self.reasons)


@dataclass(frozen=True)
class ChangeRisk:
    """The risk verdict for a whole change (a PR or a commit)."""

    level: RiskLevel
    files: list[FileRisk] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)

    @property
    def notable_files(self) -> list[FileRisk]:
        """Files worth a human's time, worst first."""
        risky = [f for f in self.files if f.level is not RiskLevel.LOW]
        return sorted(risky, key=lambda f: _SEVERITY[f.level], reverse=True)

    def summary(self) -> str:
        lines = [f"Risk: {self.level.value} across {len(self.files)} changed file(s)"]
        lines += [f"  - {reason}" for reason in self.reasons]
        lines += [f"  {risk.describe()}" for risk in self.notable_files]
        lines += [f"  (could not check: {item})" for item in self.unchecked]
        return "\n".join(lines)


def is_test_file(path: str, config: RiskConfig) -> bool:
    """True if this path looks like a test."""
    normalised = normalise_path(path)
    name = PurePosixPath(normalised).name
    return any(
        matches(normalised, pattern) or matches(name, pattern) for pattern in config.test_patterns
    )


def is_source_file(path: str, config: RiskConfig) -> bool:
    """True if this is code we would expect tests to cover."""
    return PurePosixPath(normalise_path(path)).suffix in config.source_extensions


def sensitive_areas(path: str, config: RiskConfig) -> list[str]:
    """Which sensitive areas this path belongs to, if any.

    Assets and docs are exempt. The keywords match anywhere in the path, so
    without this a stylesheet at 'admin/css/widgets.css' would be reported as
    permissions code purely because 'admin' appears in its path — a real false
    positive found while running this against django/django.
    """
    normalised = normalise_path(path)
    if PurePosixPath(normalised).suffix in config.ignore_extensions:
        return []

    return sorted(
        area
        for area, patterns in config.sensitive_paths.items()
        if any(matches(normalised, pattern) for pattern in patterns)
    )


def _size_findings(changed: ChangedFile, config: RiskConfig) -> list[tuple[RiskLevel, str]]:
    total = changed.total_changes
    if total >= config.huge_file_changes:
        return [(RiskLevel.HIGH, f"{total} lines changed in one file - too large to review carefully")]
    if total >= config.large_file_changes:
        return [(RiskLevel.MEDIUM, f"{total} lines changed in one file - a big diff to review")]
    return []


def assess_file(changed: ChangedFile, config: RiskConfig, churn: int | None = None) -> FileRisk:
    """Score one changed file. Pure — churn is passed in, not fetched here."""
    findings: list[tuple[RiskLevel, str]] = []

    areas = sensitive_areas(changed.path, config)
    if areas:
        findings.append((RiskLevel.HIGH, f"touches {' and '.join(areas)} code"))

    findings.extend(_size_findings(changed, config))

    if changed.status == "removed":
        findings.append((RiskLevel.MEDIUM, "file was deleted"))

    if churn is not None and churn >= config.high_churn_commits:
        # We only ever look back `churn_lookback` commits, so hitting that
        # number means "at least", not "exactly". Saying "30 commits" when the
        # real figure could be 300 would be quietly wrong.
        count = f"at least {churn}" if churn >= config.churn_lookback else str(churn)
        findings.append(
            (RiskLevel.MEDIUM, f"changed in {count} recent commits - this file is unstable")
        )

    return FileRisk(
        path=changed.path,
        level=highest([level for level, _ in findings]),
        reasons=[reason for _, reason in findings],
    )


def _missing_test_reason(changed_files: list[ChangedFile], config: RiskConfig) -> str | None:
    """The missing-test heuristic, over the change as a whole."""
    source_changed = [f for f in changed_files if is_source_file(f.path, config) and not is_test_file(f.path, config)]
    if not source_changed:
        return None
    if any(is_test_file(f.path, config) for f in changed_files):
        return None

    count = len(source_changed)
    noun = "file" if count == 1 else "files"
    return f"{count} source {noun} changed with no test file touched"


def _scale_reasons(changed_files: list[ChangedFile], config: RiskConfig) -> list[tuple[RiskLevel, str]]:
    findings = []
    if len(changed_files) >= config.many_files:
        findings.append((RiskLevel.MEDIUM, f"{len(changed_files)} files in one change - hard to review as a unit"))

    total = sum(f.total_changes for f in changed_files)
    if total >= config.large_change_total:
        findings.append((RiskLevel.MEDIUM, f"{total} lines changed in total"))
    return findings


def assess_change(
    changed_files: list[ChangedFile],
    config: RiskConfig,
    churn: dict[str, int] | None = None,
    unchecked: list[str] | None = None,
) -> ChangeRisk:
    """Score a whole change. Pure — all data is passed in.

    Kept free of I/O so it can be tested against mock file lists without a
    network (instructions.md #5). `analyze_pull_request` does the fetching.
    """
    churn = churn or {}
    if not changed_files:
        return ChangeRisk(level=RiskLevel.LOW, reasons=["no files changed"], unchecked=list(unchecked or []))

    file_risks = [assess_file(f, config, churn.get(f.path)) for f in changed_files]
    findings = _scale_reasons(changed_files, config)

    missing_tests = _missing_test_reason(changed_files, config)
    if missing_tests:
        findings.append((RiskLevel.MEDIUM, missing_tests))

    level = highest([risk.level for risk in file_risks] + [level for level, _ in findings])
    return ChangeRisk(
        level=level,
        files=file_risks,
        reasons=[reason for _, reason in findings],
        unchecked=list(unchecked or []),
    )


def analyze_pull_request(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    config: RiskConfig,
    churn_file_limit: int = 10,
) -> ChangeRisk:
    """Fetch a pull request and score it.

    Churn is only checked for the files that already look risky, and only for
    the first `churn_file_limit` of them — one API call per file would otherwise
    burn the rate limit on a large PR for a secondary signal.
    """
    changed_files = client.pull_request_files(owner, repo, number)
    unchecked: list[str] = []
    churn: dict[str, int] = {}

    interesting = [f for f in changed_files if sensitive_areas(f.path, config)] or changed_files
    for changed in interesting[:churn_file_limit]:
        try:
            churn[changed.path] = client.commit_count_for_path(
                owner, repo, changed.path, limit=config.churn_lookback
            )
        except GitHubError as exc:
            unchecked.append(f"churn history ({exc})")
            break

    return assess_change(changed_files, config, churn=churn, unchecked=unchecked)
