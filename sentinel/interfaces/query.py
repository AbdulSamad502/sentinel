"""Answering questions about a watched repo, in the words a person uses.

"What went wrong?", "Why did you stop it?", "Can we ship?" - answered from the
state Sentinel already has on disk. This module is Phase 4's whole point, and
the seam Phase 5 swaps: the bot calls `answer()` and nothing else, so hosting
the orchestrator on AgentCore changes this file and not the interfaces above it,
exactly the way `llm.py` isolates the model provider.

**The model does not decide anything here.** A question is routed to an intent by
deterministic keyword matching - free, predictable, testable - and each intent is
answered from an `Assessment` that `synthesize()` already decided. Only a question
that matches nothing reaches the model at all, and even then the model is handed
the *finished report* and asked to find the answer in it. It never sees raw
signals, so it can never invent a verdict from them. That is the trap CLAUDE.md
warns about, closed structurally rather than by asking the model nicely.

Nothing here raises. A chat bot that dies on an unusual question is worse than
one that says it did not understand (instructions.md #7).

    python -m sentinel.interfaces.query <repo> "can we ship?"
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from ..explainer.config import load_explain_config
from ..explainer.diff import build_changes, recoveries, recovery_report, summarise
from ..llm import run_agent
from ..norms.checker import correction_request
from ..orchestrator.review import review_session
from ..session import open_session
from ..shared.config_file import ConfigError, resolve_config_path

VERDICT = "verdict"
WHAT_CHANGED = "what_changed"
WHY = "why"
RULES = "rules"
FIX = "fix"
STATUS = "status"
HELP = "help"
ASK = "ask"  # matched nothing; the model answers from the finished report

# Checked in order, first match wins, so the more specific phrasings come first:
# "what went wrong" must reach WHY rather than WHAT_CHANGED.
_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (HELP, ("/help", "/start", "what can you do", "what can i ask", "commands")),
    (FIX, ("/fix", "how do i fix", "how to fix", "what do i tell", "tell it to fix", "make it right", "correction")),
    (STATUS, ("/status", "are you watching", "still watching", "status")),
    (RULES, ("/rules", "rule", "norm", "convention", "guideline", "standard")),
    (WHY, ("/why", "why", "what went wrong", "whats wrong", "what's wrong", "what is wrong")),
    (VERDICT, (
        "/verdict", "can we ship", "can i ship", "should i ship", "ship it", "safe to",
        "is it safe", "should i merge", "can i merge", "ok to merge", "good to go", "verdict",
    )),
    (WHAT_CHANGED, (
        "/changes", "what did", "what changed", "what has changed", "what happened",
        "what's changed", "whats changed", "summarise", "summarize", "diff",
    )),
]

HELP_TEXT = """I am watching your repo and can answer these:

  What did it just do?      - a plain description of the change
  Can we ship it?           - the verdict, and why
  Why did you stop it?      - what went wrong
  Did it follow my rules?   - which project rules the change breaks
  How do I fix it?          - a message you can send the coding agent
  Are you watching?         - session status

Ask in your own words, or use /changes /verdict /why /rules /fix /status."""

_NO_SESSION = (
    "I have no recorded session for this repo, so I cannot say what happened.\n"
    "Start one with:  python -m sentinel.action_monitor.watcher <repo>"
)

FREEFORM_PROMPT = """You are Sentinel, a supervisor watching an AI coding agent.

You will be given a developer's question and a report that has ALREADY been
decided, then asked to answer the question from that report.

Rules:
- Answer only from the report. Never add a finding or a verdict it does not state.
- If the report does not answer the question, say so plainly and say what it does cover.
- Never soften or upgrade the verdict. Report the one you were given.
- No preamble, no bullet points, no markdown. 2-4 plain sentences."""


@dataclass(frozen=True)
class Answer:
    """What to say back, and which question this was understood as.

    `intent` is exposed so an interface can log or test the routing without
    parsing the prose, and so a caller can tell a real answer from a fallback.
    """

    text: str
    intent: str


def classify(question: str) -> str:
    """Which question this is. Pure, deterministic, and free.

    Deliberately keyword matching rather than a model call: routing is the one
    part of a chat interface that must behave identically every time, and a
    developer asking "can we ship?" should never spend a token to find out.
    """
    text = " ".join(question.lower().replace("?", " ").replace("!", " ").split())
    for intent, phrases in _KEYWORDS:
        if any(phrase in text for phrase in phrases):
            return intent
    return ASK


def answer(question: str, root: Path, config_path: Path | None = None) -> Answer:
    """Answer a question about `root`, from what Sentinel recorded.

    Signals are gathered per intent rather than all at once, so asking "what did
    it do?" does not pay for a norm check, and only the intents that need the
    verdict pay for one.
    """
    intent = classify(question)
    root = root.resolve()
    config_path = config_path or resolve_config_path(root)

    try:
        if intent == HELP:
            return Answer(HELP_TEXT, intent)
        if intent == STATUS:
            return Answer(_status(root, config_path), intent)
        if intent == WHAT_CHANGED:
            return Answer(_what_changed(root, config_path), intent)
        if intent == ASK:
            return Answer(_freeform(question, root, config_path), intent)
        return Answer(_from_assessment(intent, root, config_path), intent)
    except ConfigError as exc:
        return Answer(f"I cannot read this project's config, so I cannot answer: {exc}", intent)
    except Exception as exc:  # noqa: BLE001 - a chat interface must not die on one question
        return Answer(f"Something went wrong answering that: {exc}", intent)


def _status(root: Path, config_path: Path | None) -> str:
    log = open_session(root, config_path)
    if not log.exists:
        return _NO_SESSION

    touched = len(log.recorded())
    lines = [f"Watching {root}.", f"Session started {log.started_at()}."]
    lines.append(f"{touched} file(s) touched so far." if touched else "Nothing has changed yet.")

    skipped = log.skipped()
    if skipped:
        lines.append(f"{len(skipped)} file(s) were too large or binary to snapshot, so I cannot explain those.")
    return "\n".join(lines)


def _what_changed(root: Path, config_path: Path | None) -> str:
    """The change itself. No verdict, so no norm check and no wasted tokens."""
    log = open_session(root, config_path)
    if not log.exists:
        return _NO_SESSION

    changes, unchecked = build_changes(log)
    if not changes:
        return "Nothing has changed since I started watching."

    # Imported here so that the intents which never narrate do not load the
    # model provider at all.
    from ..explainer.explain import explain_session

    summary = summarise(changes, unchecked)
    narration = explain_session(changes, unchecked, load_explain_config(config_path))

    # `explain_session` falls back to exactly this summary when the model is
    # unavailable, so prepending it unconditionally would print it twice.
    if narration.startswith(summary):
        return narration
    return f"{summary}\n\n{narration}"


def _from_assessment(intent: str, root: Path, config_path: Path | None) -> str:
    """The intents that need a decided verdict: verdict, why, rules, fix."""
    if not open_session(root, config_path).exists:
        return _NO_SESSION

    assessment, _changes, findings = review_session(root, config_path)

    if intent == VERDICT:
        return assessment.report()

    if intent == WHY:
        if not assessment.reasons and not assessment.unchecked:
            return f"{assessment.headline}\n\nNothing was flagged."
        lines = [assessment.headline, ""]
        lines += [f"- {reason}" for reason in assessment.reasons]
        if assessment.unchecked:
            lines.append("")
            lines.append("I also could not check:")
            lines += [f"- {item}" for item in assessment.unchecked]
            lines.append("")
            lines.append("An unverified check is not a pass.")
        return "\n".join(lines)

    if intent == RULES:
        if not findings:
            return "The change breaks none of the rules I was able to check.\n\n" + assessment.report()
        lines = [f"The change breaks {len(findings)} of this project's rule(s):", ""]
        lines += [f"- {finding.describe()}" for finding in findings]
        return "\n".join(lines)

    # FIX
    parts = []
    if findings:
        parts.append("Send this to the coding agent. I will not send it for you:\n\n" + correction_request(findings))

    # Anything the agent deleted is probably still recoverable from the
    # baseline snapshot, and that is the most urgent thing to say.
    restorable = recovery_report(recoveries(open_session(root, config_path), _changes_only(root, config_path)))
    if restorable:
        parts.append(restorable)

    if not parts:
        return "Nothing to correct - the change breaks none of the rules I could check."
    return "\n\n".join(parts)


def _changes_only(root: Path, config_path: Path | None):
    """The file changes alone, with no verdict and no model call."""
    changes, _unchecked = build_changes(open_session(root, config_path))
    return changes


def _freeform(question: str, root: Path, config_path: Path | None) -> str:
    """A question we did not recognise, answered from the finished report.

    The model sees the verdict and its reasons, never the raw signals, so it can
    restate but cannot decide. If it is unavailable, the report itself is still
    a useful answer.
    """
    if not open_session(root, config_path).exists:
        return _NO_SESSION

    assessment, _changes, _findings = review_session(root, config_path)
    report = assessment.report()

    stand = f"I did not quite follow that. Here is where things stand:\n\n{report}"
    try:
        response = run_agent(FREEFORM_PROMPT, f"Question: {question}\n\nReport:\n{report}")
    except Exception:  # noqa: BLE001 - a chat interface must not die on a model hiccup
        return stand

    return response or stand


def main(argv: list[str] | None = None) -> int:
    """Ask from a terminal, so the interface can be tried without a bot token."""
    parser = argparse.ArgumentParser(description="Ask Sentinel about a watched repo.")
    parser.add_argument("path", help="the watched repo")
    parser.add_argument("question", nargs="*", help="what to ask (default: show the help text)")
    parser.add_argument("--intent", action="store_true", help="print how the question was routed")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    result = answer(" ".join(args.question) or "/help", root)
    if args.intent:
        print(f"[{result.intent}]")
    print(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
