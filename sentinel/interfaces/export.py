"""The whole session as one JSON document, for the dashboard to read.

A dashboard is a viewer over session data, so this is the only thing standing
between the two. Everything here already exists elsewhere - `review_session`
decides, `build_changes` reconstructs, `recoveries` finds what can be restored -
and this assembles the results. It is a serializer, not a second brain
(instructions.md #2).

The same document serves both places the dashboard runs:

  * hosted, as a static file committed to the repo, so a judge can click a link
    and see a real session with no setup at all;
  * locally, served live by `dashboard.py` against the repo being watched.

Keys stay snake_case, matching the Python that produces them. Translating cases
at the boundary is one more thing to get wrong for no gain.

    python -m sentinel.interfaces.export <repo> > session.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..explainer.diff import FileChange, recoveries, truncate_diff
from ..norms.norms import NormFinding
from ..orchestrator.review import review_session
from ..orchestrator.verdict import Assessment
from ..session import SessionLog, open_session
from ..shared.config_file import ConfigError, resolve_config_path

# Generous, but bounded. A session is capped at 200KB per file already, yet one
# generated file could still bloat the document past what a browser enjoys.
MAX_DIFF_LINES = 1000


def _change(change: FileChange) -> dict:
    diff = truncate_diff(change.diff, MAX_DIFF_LINES)
    return {
        "path": change.path,
        "status": change.status,
        "added": len(change.added_lines),
        "removed": len(change.removed_lines),
        "diff": diff,
        "diff_truncated": diff != change.diff,
    }


def _finding(finding: NormFinding) -> dict:
    return {
        "norm_id": finding.norm_id,
        "statement": finding.statement,
        "path": finding.path,
        "line": finding.line,
        "severity": finding.severity.value,
        "source": finding.source,
        "evidence": finding.evidence,
    }


def _verdict(assessment: Assessment) -> dict:
    return {
        "level": assessment.verdict.value,
        "headline": assessment.headline,
        "reasons": list(assessment.reasons),
        "conditions": list(assessment.conditions),
        "unchecked": list(assessment.unchecked),
    }


def build_document(root: Path, config_path: Path | None = None, use_model: bool = False) -> dict:
    """Everything the dashboard needs, in one object.

    `use_model` defaults to **False** because the dashboard rebuilds this every
    few seconds while an agent is watching. Judging the written norms takes
    about a minute of inference, so doing it on a timer would spend the whole
    budget answering a question nobody asked. Those norms come back as
    "could not check", which caps the verdict at REVIEW and is exactly what the
    reader should be told - the rules check is a button, and it runs them.
    """
    config_path = config_path or resolve_config_path(root)
    log = open_session(root, config_path)

    if not log.exists:
        return {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repo": {"root": str(root), "name": root.name},
            "session": {"recorded": False},
            "verdict": None,
            "changes": [],
            "findings": [],
            "recoveries": [],
            "consultations": [],
            "timeline": [],
        }

    assessment, changes, findings = review_session(root, config_path, use_model)

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": {"root": str(root), "name": root.name},
        "session": {
            "recorded": True,
            "started": log.started_at(),
            "files_touched": len(log.recorded()),
            "skipped": log.skipped(),
        },
        "verdict": _verdict(assessment),
        "changes": [_change(change) for change in changes],
        "findings": [_finding(finding) for finding in findings],
        "recoveries": [
            {"path": item.path, "command": item.command()} for item in recoveries(log, changes)
        ],
        "consultations": log.consultations(),
        "timeline": log.timeline(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a watched session as JSON for the dashboard.")
    parser.add_argument("path", nargs="?", default=".", help="the watched repo (default: current)")
    parser.add_argument("-o", "--output", type=Path, default=None, help="write here instead of stdout")
    parser.add_argument("--config", type=Path, default=None, help="path to sentinel.config.json")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="also run the norms only a model can judge (slow: about a minute)",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    try:
        document = build_document(root, args.config, args.judge)
    except ConfigError as exc:
        print(f"Cannot export: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(document, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
