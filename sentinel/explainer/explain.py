"""Explaining a recorded session in plain English.

An agent can rewrite twelve files in ninety seconds. Reading that by hand is
slow, and asking the coding agent to summarise its own work costs tokens and
asks the suspect to write the report. Sentinel was watching independently, so
it can answer from what it actually observed.

The model's job here is narrow, exactly as in `orchestrator/agent.py`: describe
what is in the diff. It does not decide whether the change is safe — that is
`verdict.synthesize()`'s job, and moving it here would put a non-deterministic
answer where a deterministic one belongs.

    python -m sentinel.explainer.explain <path-to-repo>
    python -m sentinel.explainer.explain <path-to-repo> --detail src/thing.py
    python -m sentinel.explainer.explain <path-to-repo> --plain
"""

import argparse
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..llm import ModelSetupError, run_agent
from ..session import open_session
from ..shared.config_file import ConfigError, resolve_config_path
from .config import ExplainConfig, load_explain_config
from .diff import FileChange, build_changes, diff_block, diff_body, summarise, truncate_diff

SESSION_PROMPT = """You are Sentinel, watching an AI coding agent work on a codebase.

You will be given the diff of what the agent changed. Tell the developer what it
did, in 3-6 sentences.

Rules:
- Describe only what is in the diff. Never guess at intent you cannot see.
- Never invent a file, function, or change that is not shown.
- Say what changed in behaviour, not just which lines moved. "Discounts are now
  subtracted before tax" beats "edited the total function".
- Do NOT say whether the change is safe, risky, or ready to ship. That verdict
  is decided elsewhere and is not your call.
- If part of the diff was not shown to you, say so rather than filling the gap.
- No preamble, no bullet points, no markdown. Plain sentences."""

FILE_PROMPT = """You are Sentinel, watching an AI coding agent work on a codebase.

You will be given the diff of one file. Explain what changed in it, in 3-6
sentences, for a developer who has not read the code.

Rules:
- Describe only what is in the diff. Never invent code that is not shown.
- Explain the effect of the change, not a line-by-line reading of it.
- Do NOT say whether the change is safe, risky, or ready to ship.
- If the diff was truncated, say that you only saw part of it.
- No preamble, no bullet points, no markdown. Plain sentences."""


def session_prompt(changes: list[FileChange], unchecked: list[str], config: ExplainConfig) -> str:
    """What the model is shown for a whole session."""
    parts = ["The agent made these changes:", "", summarise(changes, unchecked)]

    shown = changes[: config.max_files]
    if shown:
        parts.append("")
        parts.append("Diffs:")
        parts += [diff_block(change, config.max_diff_lines) for change in shown]

    hidden = len(changes) - len(shown)
    if hidden > 0:
        parts.append(f"\n({hidden} more changed file(s) were not shown to you.)")

    return "\n".join(parts)


def _narrate(system_prompt: str, build_prompt: Callable[[], str], fallback: str) -> str:
    """One model call, with the fallback the whole design depends on.

    A model hiccup must never cost the answer: the deterministic summary is
    already correct, and narration is an improvement on it (instructions.md #7).

    The prompt is built by a callback so that building it happens *inside* the
    guard. A truncation bug while assembling a 40-file diff would otherwise
    escape and take the answer with it.

    The model is reached here rather than at module scope so that `--plain` --
    which promises to spend no tokens -- also loads no model provider, the same
    way `review.py` imports `agent.explain` only inside its `--explain` branch.
    """
    try:
        text = run_agent(system_prompt, build_prompt())
    except ModelSetupError as exc:
        return f"{fallback}\n\n(No plain-English explanation: {exc})"
    except Exception as exc:  # noqa: BLE001 - a demo must not die on a model hiccup
        return f"{fallback}\n\n(No plain-English explanation: the model call failed - {exc})"

    return text or fallback


def explain_session(changes: list[FileChange], unchecked: list[str] | None = None, config: ExplainConfig | None = None) -> str:
    """A short narrative of everything the agent changed."""
    unchecked = unchecked or []
    config = config or ExplainConfig()
    fallback = summarise(changes, unchecked)

    if not changes:
        return fallback
    return _narrate(SESSION_PROMPT, lambda: session_prompt(changes, unchecked, config), fallback)


def explain_file(change: FileChange, config: ExplainConfig | None = None) -> str:
    """A deeper explanation of one file's change."""
    settings = config or ExplainConfig()

    def prompt() -> str:
        return f"File: {change.path} ({change.status})\n\n{diff_body(change, settings.max_diff_lines)}"

    return _narrate(FILE_PROMPT, prompt, change.describe())


_RELATIVE_UNITS = {"m": 60, "h": 3600, "d": 86400}


def since_timestamp(value: str, now: datetime | None = None) -> str:
    """Turn "30m", "2h", "1d" or an ISO time into a timestamp to compare against.

    Relative forms are what a person actually types when they get back to their
    desk. Anything unrecognised is returned as-is, so a full ISO timestamp
    still works.
    """
    value = value.strip()
    if not value:
        return ""

    unit = value[-1].lower()
    if unit in _RELATIVE_UNITS and value[:-1].isdigit():
        moment = (now or datetime.now(timezone.utc)) - timedelta(seconds=int(value[:-1]) * _RELATIVE_UNITS[unit])
        return moment.isoformat(timespec="seconds")
    return value


def format_timeline(events: list[dict]) -> str:
    """The session as a sequence, for "what happened while I was away"."""
    if not events:
        return "Nothing recorded in that window."

    markers = {"BLOCKED": "STOP", "FLAGGED": "FLAG", "ALLOWED": "ok  ", "": "    "}
    lines = []
    for event in events:
        # Time only: a session is one sitting, so the date is noise.
        clock = event["at"][11:19] if len(event["at"]) >= 19 else event["at"]
        lines.append(f"{clock}  [{markers.get(event['verdict'], '    ')}] {event['what']}")
        lines += [f"                     - {reason}" for reason in event["reasons"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explain what a coding agent changed in a watched repo.")
    parser.add_argument("path", nargs="?", default=".", help="the watched repo (default: current)")
    parser.add_argument("--detail", metavar="FILE", default=None, help="explain one file in depth")
    parser.add_argument("--plain", action="store_true", help="deterministic summary only - no model, no tokens")
    parser.add_argument("--timeline", action="store_true", help="what happened, in order, with timestamps")
    parser.add_argument("--since", metavar="WHEN", default="", help="only from this point: 30m, 2h, 1d, or an ISO time")
    parser.add_argument("--config", type=Path, default=None, help="path to sentinel.config.json")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    config_path = args.config or resolve_config_path(root)
    try:
        log = open_session(root, config_path)
        config = load_explain_config(config_path)
    except ConfigError as exc:
        print(f"\nCannot explain: {exc}\n", file=sys.stderr)
        return 1

    if not log.exists:
        print(
            f"No recorded session in {root}.\n"
            f"Start one first:\n  python -m sentinel.action_monitor.watcher {root}",
            file=sys.stderr,
        )
        return 1

    if args.timeline or args.since:
        print(format_timeline(log.timeline(since_timestamp(args.since))))
        return 0

    changes, unchecked = build_changes(log)

    if args.detail:
        return _report_one(args.detail, changes, config, plain=args.plain)

    print(f"Watching started {log.started_at()}.\n")
    print(summarise(changes, unchecked))
    if not args.plain and changes:
        print("\nWhat the agent did:\n")
        print(explain_session(changes, unchecked, config))
    return 0


def _report_one(wanted: str, changes: list[FileChange], config: ExplainConfig, plain: bool) -> int:
    match = next((change for change in changes if change.path == wanted), None)
    if match is None:
        print(f"No recorded change to '{wanted}' in this session.", file=sys.stderr)
        if changes:
            print("Changed in this session:", file=sys.stderr)
            for change in changes:
                print(f"  {change.path}", file=sys.stderr)
        return 1

    print(match.describe())
    print()
    print(truncate_diff(match.diff, config.max_diff_lines))
    if not plain:
        print("\nWhat this change does:\n")
        print(explain_file(match, config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
