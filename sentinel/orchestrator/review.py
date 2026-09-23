"""Gathers every signal about a change and produces one verdict.

This is the I/O half of the orchestrator: it fetches, and `verdict.synthesize()`
decides. Splitting them keeps the decision rules testable without a network.

Each signal is gathered in isolation. If the risk analysis fails, CI is still
checked; if CI fails, the risk score still counts. A signal that could not be
read is recorded as unverified and caps the verdict at REVIEW — it never
silently reads as a pass (instructions.md #7).

    python -m sentinel.orchestrator.review django/django 21793
    python -m sentinel.orchestrator.review django/django 21793 --explain
"""

import argparse
import sys
from pathlib import Path

from ..action_monitor.actions import Decision
from ..code_risk.analyzer import ChangeRisk, analyze_pull_request, assess_change, sensitive_areas
from ..code_risk.churn import local_churn
from ..code_risk.config import load_risk_config
from ..explainer.config import load_explain_config
from ..explainer.diff import ADDED, DELETED, FileChange, build_changes
from ..norms.checker import check_patterns, check_statements
from ..norms.config import load_norms
from ..norms.norms import NormFinding
from ..session import open_session
from ..shared.config_file import ConfigError, resolve_config_path
from ..shared.github import ChangedFile, GitHubClient, GitHubError, parse_repo
from .verdict import Assessment, CIStatus, synthesize

# GitHub's vocabulary for a finished check, mapped to ours. Anything absent
# from here is treated as still running.
_FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
_PASSING_CONCLUSIONS = {"success", "neutral", "skipped"}


def interpret_checks(conclusions: list[str]) -> CIStatus:
    """Turn GitHub's per-check conclusions into one CI status.

    One failure fails the change - that is what a red build means.
    """
    if not conclusions:
        return CIStatus.NONE
    if any(conclusion in _FAILING_CONCLUSIONS for conclusion in conclusions):
        return CIStatus.FAILING
    if all(conclusion in _PASSING_CONCLUSIONS for conclusion in conclusions):
        return CIStatus.PASSING
    return CIStatus.PENDING


def gather_risk(
    client: GitHubClient, owner: str, repo: str, number: int, unchecked: list[str]
) -> ChangeRisk | None:
    try:
        return analyze_pull_request(client, owner, repo, number, load_risk_config())
    except (GitHubError, ConfigError) as exc:
        unchecked.append(f"code risk analysis ({exc})")
        return None


def gather_ci(
    client: GitHubClient, owner: str, repo: str, number: int, unchecked: list[str]
) -> CIStatus:
    try:
        head = client.pull_request_head_sha(owner, repo, number)
        return interpret_checks(client.check_conclusions(owner, repo, head))
    except GitHubError as exc:
        unchecked.append(f"CI status ({exc})")
        return CIStatus.UNKNOWN


def gather_norms(
    changes: list[FileChange], config_path: Path | None, unchecked: list[str], use_model: bool = True
) -> list[NormFinding] | None:
    """Check a change against the project's norms, in isolation from everything else.

    Returns None only when the norms could not be loaded at all — the project
    has rules and we do not know whether they were followed, which is exactly
    the state `synthesize()` refuses to call SAFE.

    `use_model=False` runs only the regex-checkable norms and puts the rest in
    `unchecked`. That is for callers that must answer immediately and often —
    the dashboard rebuilds this document every few seconds, and a model call on
    that path would spend a minute of inference per poll. Saying "not checked
    here" is honest and caps the verdict at REVIEW; quietly returning fewer
    findings would read as a pass.
    """
    try:
        norms = load_norms(config_path)
    except ConfigError as exc:
        unchecked.append(f"this project's rules ({exc})")
        return None

    if not norms:
        # No norms configured means there is nothing to check, which is not
        # the same as failing to check something.
        return []

    findings = check_patterns(changes, norms)

    judged_norms = [norm for norm in norms if not norm.is_deterministic]
    if not use_model:
        if judged_norms:
            names = ", ".join(norm.id for norm in judged_norms)
            unchecked.append(
                f"the project norms that need judgement ({names}) - not checked here, "
                "ask for a rules check to run them"
            )
        return findings

    try:
        max_lines = load_explain_config(config_path).max_diff_lines
    except ConfigError:
        max_lines = 120

    judged, judged_unchecked = check_statements(changes, norms, max_lines)
    unchecked.extend(judged_unchecked)
    return findings + judged


# GitHub's word for a deleted file. `assess_file` tests against it directly, so
# a local change has to speak the same vocabulary to be scored the same way.
_GITHUB_STATUS = {ADDED: "added", DELETED: "removed"}


def as_changed_files(changes: list[FileChange]) -> list[ChangedFile]:
    """Local file changes in the shape the Code Risk Analyzer already reads.

    The analyzer was written against GitHub's `ChangedFile` and is pure, so it
    scores a local session perfectly well once the diff is described the same
    way. This is a translation, not a second implementation of the heuristics
    (instructions.md #2).
    """
    return [
        ChangedFile(
            path=change.path,
            status=_GITHUB_STATUS.get(change.status, "modified"),
            additions=len(change.added_lines),
            deletions=len(change.removed_lines),
        )
        for change in changes
    ]


def gather_local_risk(
    root: Path, changes: list[FileChange], config_path: Path | None, unchecked: list[str]
) -> ChangeRisk | None:
    """Score a local session for risk, in isolation from the other signals.

    All four signals work here. Sensitive paths, missing tests and diff size
    come straight from the diff; churn is read from the local git history
    rather than left as a standing caveat, because a warning that appears on
    every clean change teaches people to ignore all of them. Only a repo
    without git, or a git that will not answer, produces one - and then it is
    a real one.

    Note a local session still cannot reach SAFE, because CI is unknown until
    the change is pushed. That is the "unverified is not a pass" rule doing its
    job, not a gap in this function.
    """
    if not changes:
        return None

    try:
        config = load_risk_config(config_path)
    except ConfigError as exc:
        unchecked.append(f"code risk analysis ({exc})")
        return None

    changed_files = as_changed_files(changes)
    # Churn is a secondary signal, so it is only looked up for the files that
    # already look risky - the same economy `analyze_pull_request` applies.
    interesting = [f.path for f in changed_files if sensitive_areas(f.path, config)] or [
        f.path for f in changed_files
    ]
    churn, churn_unchecked = local_churn(root, interesting, config.churn_lookback)

    return assess_change(changed_files, config, churn=churn, unchecked=churn_unchecked)


def review_session(
    root: Path, config_path: Path | None = None, use_model: bool = True
) -> tuple[Assessment, list[FileChange], list[NormFinding]]:
    """Assess what a coding agent did to a local repo, from the recorded session.

    This is the local counterpart to `review_pull_request`, and the entry point
    a chat interface should call: everything it needs is already on disk, so
    answering "can we ship?" costs one norm check and no network at all.

    `use_model=False` skips the norms only a model can judge and names them as
    unchecked. Pass it from anything that refreshes on a timer - see
    `gather_norms`. The verdict stays deterministic either way; it simply knows
    less, and says so.

    Returns the assessment along with the change and the findings, so a caller
    can explain the change without rebuilding any of it.
    """
    config_path = config_path or resolve_config_path(root)
    unchecked: list[str] = []

    log = open_session(root, config_path)
    if not log.exists:
        # Nothing was recorded, so we know nothing. `decisions=None` and
        # `norm_findings=None` both say so, and the verdict cannot be SAFE.
        return synthesize(decisions=None, norm_findings=None, unchecked=unchecked), [], []

    changes, change_unchecked = build_changes(log)
    unchecked.extend(change_unchecked)

    findings = gather_norms(changes, config_path, unchecked, use_model)
    risk = gather_local_risk(root, changes, config_path, unchecked)
    assessment = synthesize(
        decisions=log.decisions(),
        norm_findings=findings,
        risk=risk,
        unchecked=unchecked,
    )
    return assessment, changes, findings or []


def review_pull_request(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    decisions: list[Decision] | None = None,
    config_path: Path | None = None,
) -> Assessment:
    """Assess a pull request end to end.

    `decisions` are the Action Monitor's findings if it was watching the agent
    that produced this change. Passing None is honest about not knowing, and is
    recorded as an unverified signal rather than assumed to be clean.
    """
    unchecked: list[str] = []
    risk = gather_risk(client, owner, repo, number, unchecked)
    ci = gather_ci(client, owner, repo, number, unchecked)

    # GitHub gives this path a file list, not the diff text, so norms cannot be
    # checked here. Saying so only matters when the project actually has norms;
    # a project with none has missed nothing.
    try:
        norm_findings = None if load_norms(config_path) else []
        if norm_findings is None:
            unchecked.append("this project's rules (a pull request review sees the file list, not the diff)")
    except ConfigError as exc:
        norm_findings = None
        unchecked.append(f"this project's rules ({exc})")

    return synthesize(
        decisions=decisions, risk=risk, ci=ci, unchecked=unchecked, norm_findings=norm_findings
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assess whether a pull request is safe to ship.")
    parser.add_argument("repo", help="owner/repo, e.g. django/django")
    parser.add_argument("number", type=int, help="pull request number")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="have the local model restate the verdict in plain English",
    )
    args = parser.parse_args(argv)

    try:
        owner, repo = parse_repo(args.repo)
    except GitHubError as exc:
        print(exc, file=sys.stderr)
        return 1

    client = GitHubClient()
    if not client.is_authenticated:
        print("No GITHUB_TOKEN set - limited to 60 requests an hour.\n")

    print(f"Reviewing {owner}/{repo} PR #{args.number}\n")
    assessment = review_pull_request(client, owner, repo, args.number)
    print(assessment.report())

    if args.explain:
        from .agent import explain

        print("\n" + "-" * 60)
        print(explain(assessment))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
