"""The vocabulary the Action Monitor speaks in.

An `Action` is one thing the coding agent did. A `Decision` is Sentinel's
response to it. Both are plain data with no behaviour, so every other module
here — config, rules, watcher — can be tested against them in isolation.
"""

from dataclasses import dataclass, field
from enum import Enum


class ActionType(str, Enum):
    """What the coding agent did.

    Inherits from `str` so these serialise straight to JSON when the
    orchestrator and the chat interface pass actions around later.
    """

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    GIT = "git"


class Verdict(str, Enum):
    """Sentinel's response to a single action.

    Deliberately narrower than the orchestrator's four-way verdict: one action
    on its own is not enough to say whether the whole change is shippable.
    """

    ALLOWED = "ALLOWED"
    FLAGGED = "FLAGGED"
    BLOCKED = "BLOCKED"


# Least to most severe. Used to pick the winner when several rules fire on one
# action — the worst finding decides the verdict, and the rest still get
# reported as reasons.
_SEVERITY = {Verdict.ALLOWED: 0, Verdict.FLAGGED: 1, Verdict.BLOCKED: 2}


def worst(verdicts: list[Verdict]) -> Verdict:
    """The most severe verdict in the list. ALLOWED if the list is empty."""
    return max(verdicts, key=lambda verdict: _SEVERITY[verdict], default=Verdict.ALLOWED)


@dataclass(frozen=True)
class Action:
    """One observed action by the coding agent.

    `path` is repo-relative and uses forward slashes on every platform, so the
    config file's patterns mean the same thing on all three teammates' laptops.
    `command` is set only for ActionType.GIT.
    """

    type: ActionType
    path: str = ""
    command: str = ""

    def describe(self) -> str:
        if self.type is ActionType.GIT:
            return f"git command: {self.command}"
        return f"{self.type.value} {self.path}"


@dataclass(frozen=True)
class Decision:
    """What Sentinel makes of one action, and why.

    `reasons` is always populated for a non-ALLOWED verdict — a flag with no
    stated reason is not useful to the human reading it.
    """

    action: Action
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.reasons:
            return f"{self.verdict.value}: {self.action.describe()}"
        # ASCII only: the default Windows console encoding mangles em dashes,
        # and this text goes straight to a terminal during the demo.
        return f"{self.verdict.value}: {self.action.describe()} - " + "; ".join(self.reasons)
