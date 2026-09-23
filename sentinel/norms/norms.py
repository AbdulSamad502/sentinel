"""The vocabulary project norms speak in.

Plain data with no behaviour, so the checker, the config loader and the wizard
can each be tested against them in isolation (instructions.md #5).

Norms reuse the Action Monitor's `Verdict` rather than inventing a fourth
vocabulary. "Was this permitted?" is exactly the question a norm asks, and
ALLOWED / FLAGGED / BLOCKED is exactly the answer. `RiskLevel` and
`ReliabilityVerdict` still mean what they meant.
"""

from dataclasses import dataclass, field

from ..action_monitor.actions import Verdict, worst

# How a finding was reached. Always shown to the human: a regex hit is a fact,
# a model's judgement is an opinion, and they should not read identically.
BY_PATTERN = "pattern"
BY_MODEL = "model"


@dataclass(frozen=True)
class Norm:
    """One rule this project holds its coding agent to.

    `statement` is the plain-English rule. It is what the wizard shows, what
    the model is asked to judge against, and what the developer reads in a
    finding, so it should be a sentence rather than a label.

    `patterns` are regexes matched against single added lines. When a norm has
    them, it is checked deterministically and the model is never involved.
    Multi-line patterns will not work by design - see `checker.check_patterns`.
    """

    id: str
    statement: str
    severity: Verdict = Verdict.FLAGGED
    patterns: list[str] = field(default_factory=list)
    applies_to: list[str] = field(default_factory=list)
    except_paths: list[str] = field(default_factory=list)

    @property
    def is_deterministic(self) -> bool:
        """True when a regex decides this norm, so no model call is needed."""
        return bool(self.patterns)


@dataclass(frozen=True)
class NormFinding:
    """One place a change broke one norm."""

    norm_id: str
    statement: str
    path: str
    severity: Verdict
    evidence: str
    source: str
    line: int = 0

    def describe(self) -> str:
        """One line, ASCII only - this goes to a terminal during the demo."""
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where} breaks '{self.norm_id}' (found by {self.source}): {self.evidence}"


def worst_severity(findings: list[NormFinding]) -> Verdict:
    """The most severe finding's verdict. ALLOWED when there are none."""
    return worst([finding.severity for finding in findings])
