"""The verdict on what a coding agent just did to a local repo.

`review.py` answers this for a pull request, over the network. This answers it
for the session the watcher recorded, entirely from disk — which is the loop a
developer actually lives in: hand the agent a task, watch, then ask whether the
result is shippable.

    python -m sentinel.orchestrator.session_review <path-to-repo>
    python -m sentinel.orchestrator.session_review <path-to-repo> --explain
    python -m sentinel.orchestrator.session_review <path-to-repo> --fix-prompt

`--fix-prompt` prints a message asking the coding agent to put its rule
breaches right. It prints it. Sending it is the human's call, and Sentinel has
no channel to the coding agent by design — it observes, analyses and reports
(instructions.md #6).
"""

import argparse
import sys
from pathlib import Path

from ..explainer.diff import recoveries, recovery_report
from ..norms.checker import correction_request
from ..session import SessionLog, open_session
from ..shared.config_file import ConfigError, resolve_config_path
from .markdown import markdown_report
from .review import review_session
from .verdict import ReliabilityVerdict


def consultation_summary(log: SessionLog) -> str:
    """What the coding agent asked Sentinel while it worked.

    Reported next to the verdict on purpose. An agent that consults its
    supervisor and quietly fixes what it finds is an agent whose mistakes the
    human never sees, so the fact that it asked is itself something to report.
    """
    entries = log.consultations()
    if not entries:
        return ""

    counts: dict[str, int] = {}
    for entry in entries:
        tool = str(entry.get("tool", "unknown"))
        counts[tool] = counts.get(tool, 0) + 1

    detail = ", ".join(f"{tool} x{count}" for tool, count in sorted(counts.items()))
    return f"\nThe coding agent consulted Sentinel {len(entries)} time(s): {detail}."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assess what a coding agent did to a watched repo.")
    parser.add_argument("path", nargs="?", default=".", help="the watched repo (default: current)")
    parser.add_argument("--explain", action="store_true", help="have the model restate the verdict in plain English")
    parser.add_argument(
        "--fix-prompt",
        action="store_true",
        help="print a message you can send the coding agent asking it to fix the rules it broke",
    )
    parser.add_argument("--markdown", action="store_true", help="emit a pull-request comment instead of terminal text")
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="exit 1 on STOP so a CI job can gate on it (your pipeline acts, not Sentinel)",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to sentinel.config.json")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    config_path = args.config or resolve_config_path(root)
    try:
        # Checked here rather than inside review_session: an unwatched repo
        # really does deserve the REVIEW verdict that function returns, and a
        # bot asking "can we ship?" should get it. A person at a terminal is
        # better served by being told what to run.
        if not open_session(root, config_path).exists:
            print(f"No recorded session in {root}.", file=sys.stderr)
            print(f"Start one first:\n  python -m sentinel.action_monitor.watcher {root}", file=sys.stderr)
            return 1
        assessment, changes, findings = review_session(root, config_path)
    except ConfigError as exc:
        print(f"\nCannot review: {exc}\n", file=sys.stderr)
        return 1

    log = open_session(root, config_path)
    restorable = recoveries(log, changes)

    if args.markdown:
        print(markdown_report(assessment, changes, findings, restorable))
        return _status_for(assessment, args.exit_code)

    print(f"{len(changes)} file(s) changed in this session.")
    consulted = consultation_summary(open_session(root, config_path))
    if consulted:
        print(consulted)
    print()

    if findings:
        print("Project rules broken:")
        for finding in findings:
            print(f"  - {finding.describe()}")
        print()

    print(assessment.report())

    # A deleted file usually is not lost: the baseline snapshot still has it.
    # Printed after the verdict because it is what the human does next.
    report = recovery_report(restorable)
    if report:
        print()
        print(report)

    if args.fix_prompt:
        print("\n" + "-" * 60)
        print("Send this to the coding agent (Sentinel will not send it for you):\n")
        print(correction_request(findings))

    if args.explain:
        from .agent import explain

        print("\n" + "-" * 60)
        print(explain(assessment))

    return _status_for(assessment, args.exit_code)


def _status_for(assessment, gate: bool) -> int:
    """The process exit status.

    Non-zero only when asked for, and only on STOP. Sentinel still reports; it
    is the pipeline's own config that turns a STOP into a failed build, which
    is a human deciding to act on a verdict rather than Sentinel acting
    (instructions.md #6).
    """
    if gate and assessment.verdict is ReliabilityVerdict.STOP:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
