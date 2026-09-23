"""Letting the coding agent consult Sentinel while it works.

Everything else in this repo answers a human. This answers the coding agent
itself, over MCP, which Claude Code, Cursor and Windsurf all speak natively.

The point is the first tool. An agent that reads the project's rules *before*
it writes does not need to be caught afterwards, and "don't hardcode model ids"
is far cheaper to obey than to undo. Detection is what Sentinel does when
prevention fails; this is the prevention.

**Sentinel still never acts.** Every tool here is read-only. The coding agent
chooses to ask, and chooses what to do about the answer - Sentinel has no way
to edit, revert or block anything, and gains none here (instructions.md #6).

**The oversight problem, and why the consultation log exists.** An agent that
can query its supervisor and fix things until it goes green is optimising to
pass Sentinel, which is not the same as writing good code - and worse, the
human stops seeing what went wrong, because it was all quietly corrected. So
every call is written to the session and reported next to the verdict: *"the
agent consulted Sentinel 5 times and self-corrected 3 rule violations."* The
human keeps oversight of a loop they are no longer in the middle of.

    python -m sentinel.interfaces.mcp_server /path/to/repo

Register it with the coding agent (Claude Code shown; Cursor is the same shape):

    {"mcpServers": {"sentinel": {
        "command": "python",
        "args": ["-m", "sentinel.interfaces.mcp_server", "/path/to/repo"]}}}

**Landmine:** stdio is the MCP transport, so stdout *is* the protocol. A stray
`print()` anywhere on these code paths corrupts the stream and the agent sees a
broken server. Diagnostics go to stderr, never stdout.
"""

import argparse
import sys
from pathlib import Path

from ..action_monitor.config import load_config
from ..explainer.diff import build_changes, summarise
from ..norms.checker import check_patterns, correction_request
from ..norms.config import load_norms
from ..norms.norms import Norm
from ..session import SessionLog, open_session
from ..shared.config_file import ConfigError, resolve_config_path

SERVER_NAME = "sentinel"

_NO_SESSION = (
    "Sentinel is not watching this repo yet, so it cannot tell you what has changed.\n"
    "Ask the developer to start it:  python -m sentinel.action_monitor.watcher <repo>\n"
    "You can still call sentinel_project_rules to find out what this project expects."
)


def _rules_text(norms: list[Norm], config) -> str:
    """The project's expectations, written for the agent about to change it."""
    lines = ["# What this project expects of you", ""]

    if norms:
        checked = [n for n in norms if n.is_deterministic]
        judged = [n for n in norms if not n.is_deterministic]

        lines.append("## Rules")
        lines.append("")
        for norm in norms:
            how = "checked automatically" if norm.is_deterministic else "judged by a reviewer"
            lines.append(f"- **{norm.statement}**")
            lines.append(f"  (`{norm.id}`, {norm.severity.value.lower()}, {how})")
        lines.append("")
        if checked:
            lines.append(
                f"{len(checked)} of these are matched against every line you add, "
                "so breaking one will be noticed."
            )
        if judged:
            verb = "needs" if len(judged) == 1 else "need"
            lines.append(f"{len(judged)} {verb} a reviewer's judgement.")
        lines.append("")
    else:
        lines.append("This project has set no explicit rules yet.\n")

    if config.protected_paths:
        lines.append("## Files you should not be changing")
        lines.append("")
        for pattern, verdict in sorted(config.protected_paths.items()):
            lines.append(f"- `{pattern}` ({verdict.value.lower()})")
        lines.append("")

    if config.dependency_patterns:
        lines.append("## Changing dependencies needs a human")
        lines.append("")
        lines.append(
            "Editing any of these is a supply-chain decision and is reported for review: "
            + ", ".join(f"`{p}`" for p in config.dependency_patterns[:8])
        )
        lines.append("")

    if config.deny:
        lines.append("## Off limits entirely")
        lines.append("")
        lines += [f"- `{pattern}`" for pattern in config.deny]
        lines.append("")

    lines.append("Follow these before you write, rather than fixing them afterwards.")
    return "\n".join(lines)


def _changed_text(log: SessionLog) -> str:
    changes, unchecked = build_changes(log)
    if not changes:
        return "You have not changed anything since Sentinel started watching."
    return summarise(changes, unchecked)


def _check_text(log: SessionLog, config_path: Path) -> str:
    """What the pattern-checkable rules make of the work so far.

    Deliberately does *not* run the rules that need a model. Those take a
    minute or more, which is useless inside an agent's edit loop - and the
    answer says so, rather than letting silence read as approval.
    """
    changes, unchecked = build_changes(log)
    if not changes:
        return "You have not changed anything since Sentinel started watching."

    norms = load_norms(config_path)
    findings = check_patterns(changes, norms)
    judged = [n for n in norms if not n.is_deterministic]

    lines = [summarise(changes, unchecked), ""]

    if findings:
        lines.append(f"## You have broken {len(findings)} of this project's rules")
        lines.append("")
        lines += [f"- {finding.describe()}" for finding in findings]
        lines.append("")
        lines.append(correction_request(findings))
    else:
        lines.append("## No rule violations found in what you added")
        lines.append("")

    if judged:
        lines.append("")
        lines.append(
            f"Not checked here: {len(judged)} rule(s) need a reviewer's judgement "
            f"({', '.join(n.id for n in judged)}). This is not a clean bill of health for them."
        )

    lines.append("")
    lines.append("Sentinel reports; it changes nothing. Fixing this is your call, and the developer sees this too.")
    return "\n".join(lines)


def build_server(root: Path, config_path: Path | None = None):
    """The MCP server for one repo. Split out so the tools can be tested."""
    from mcp.server.fastmcp import FastMCP

    resolved_config = config_path or resolve_config_path(root)
    server = FastMCP(SERVER_NAME)

    def _log(tool: str, detail: str = "") -> SessionLog:
        log = open_session(root, resolved_config)
        log.record_consultation(tool, detail)
        return log

    @server.tool(
        description=(
            "The rules this project holds you to, plus the files you must not change. "
            "Call this BEFORE writing code, not after - it is cheaper to follow a rule "
            "than to undo breaking one."
        )
    )
    def sentinel_project_rules() -> str:
        try:
            log = _log("project_rules")
            text = _rules_text(load_norms(resolved_config), load_config(resolved_config))
        except ConfigError as exc:
            return f"Sentinel could not read this project's config: {exc}"
        if log.problems:
            text += "\n\n(Sentinel note: " + "; ".join(log.problems) + ")"
        return text

    @server.tool(
        description=(
            "A summary of every file you have changed since Sentinel started watching, "
            "with line counts. Useful for re-orienting in a long session."
        )
    )
    def sentinel_what_changed() -> str:
        try:
            log = _log("what_changed")
            return _changed_text(log) if log.exists else _NO_SESSION
        except ConfigError as exc:
            return f"Sentinel could not read this project's config: {exc}"

    @server.tool(
        description=(
            "Check your work so far against this project's rules, before you claim to be "
            "done. Returns any rules you have broken and what to do about them. "
            "Read-only: Sentinel does not change your code."
        )
    )
    def sentinel_check_my_work() -> str:
        try:
            log = _log("check_my_work")
            return _check_text(log, resolved_config) if log.exists else _NO_SESSION
        except ConfigError as exc:
            return f"Sentinel could not read this project's config: {exc}"

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Let a coding agent consult Sentinel over MCP.")
    parser.add_argument("path", nargs="?", default=".", help="the repo being worked on (default: current)")
    parser.add_argument("--config", type=Path, default=None, help="path to sentinel.config.json")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        # stderr, never stdout: stdout is the MCP protocol stream.
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    try:
        server = build_server(root, args.config)
    except ImportError:
        print("The MCP SDK is not installed. It ships with strands-agents:\n  pip install -r requirements.txt", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 1

    print(f"Sentinel MCP server ready for {root}", file=sys.stderr)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
