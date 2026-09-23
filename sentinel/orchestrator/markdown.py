"""The verdict as markdown, for pasting into a pull request.

Sentinel does not post anything. `GitHubClient` is read-only and a test asserts
it has no write methods, so this produces *text* and a human - or the project's
own CI job, configured by a human - decides what to do with it
(instructions.md #6).

    python -m sentinel.orchestrator.session_review <repo> --markdown

Kept separate from `Assessment.report()` because the two audiences want
different things: `report()` is read in a terminal and stays ASCII, this is read
on GitHub and can use tables and detail blocks.
"""

from ..explainer.diff import FileChange, Recovery
from ..norms.norms import NormFinding
from .verdict import Assessment, ReliabilityVerdict

_BADGE = {
    ReliabilityVerdict.SAFE: "SAFE",
    ReliabilityVerdict.REVIEW: "REVIEW",
    ReliabilityVerdict.CONDITIONAL: "CONDITIONAL",
    ReliabilityVerdict.STOP: "STOP",
}


def markdown_report(
    assessment: Assessment,
    changes: list[FileChange] | None = None,
    findings: list[NormFinding] | None = None,
    recoverable: list[Recovery] | None = None,
) -> str:
    """One pasteable comment covering everything Sentinel found."""
    changes = changes or []
    findings = findings or []
    recoverable = recoverable or []

    lines = [f"## Sentinel: {_BADGE[assessment.verdict]}", "", assessment.headline, ""]

    if assessment.reasons:
        lines.append("**Why**")
        lines += [f"- {_one_line(reason)}" for reason in assessment.reasons]
        lines.append("")

    if assessment.conditions:
        lines.append("**Ship only once**")
        lines += [f"- {_one_line(condition)}" for condition in assessment.conditions]
        lines.append("")

    if findings:
        lines.append("**Project rules broken**")
        lines.append("")
        lines.append("| Rule | Where | Found by | Evidence |")
        lines.append("| --- | --- | --- | --- |")
        for finding in findings:
            where = f"{finding.path}:{finding.line}" if finding.line else finding.path
            lines.append(f"| `{finding.norm_id}` | `{where}` | {finding.source} | `{_cell(finding.evidence)}` |")
        lines.append("")

    if changes:
        added = sum(len(change.added_lines) for change in changes)
        removed = sum(len(change.removed_lines) for change in changes)
        lines.append("<details>")
        lines.append(f"<summary>{len(changes)} file(s) changed, +{added} -{removed}</summary>")
        lines.append("")
        for change in changes:
            lines.append(f"- `{change.path}` ({change.status}, +{len(change.added_lines)} -{len(change.removed_lines)})")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    if recoverable:
        lines.append("**Deleted files that can be restored**")
        lines.append("")
        lines.append("```")
        lines += [item.command() for item in recoverable]
        lines.append("```")
        lines.append("")

    if assessment.unchecked:
        # As prominent as everything else. The whole tool rests on an
        # unverified check never reading as a pass.
        lines.append("**Could not verify**")
        lines += [f"- {_one_line(item)}" for item in assessment.unchecked]
        lines.append("")
        lines.append("_An unverified check is not a pass._")
        lines.append("")

    lines.append("<sub>Sentinel observes, analyses and reports. It does not change your code.</sub>")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    """Collapse to a single line.

    Some reasons carry their own line breaks - an Ollama error that says how to
    start it, for instance. In a terminal those are indented under the bullet;
    in markdown a bare newline ends the list item and the remainder renders as a
    stray paragraph.
    """
    return " ".join(str(text).split())


def _cell(text: str) -> str:
    """Make a value safe to sit inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()
