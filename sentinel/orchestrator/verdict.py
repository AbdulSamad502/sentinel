"""Combines every signal Sentinel has into one answer: can we ship this?

This is the synthesis step. It takes what the Action Monitor saw, what the Code
Risk Analyzer scored, and whether CI passed, and returns one of four verdicts
with plain-English reasons.

**The verdict is computed here, not by the LLM.** A supervisor that returns
SAFE on one run and STOP on the next for identical input is not a supervisor.
The rules below are deterministic and testable; the language model's job
(`agent.py`) is to explain the result in plain English, never to decide it.

Two principles that shape every rule:

  * A check that could not run is not a pass. Anything unverified caps the
    verdict at REVIEW, so silence never reads as safety (instructions.md #7).
  * Sentinel reports. STOP means "this should not ship as it stands", not
    "Sentinel has blocked it" (instructions.md #6).
"""

from dataclasses import dataclass, field
from enum import Enum

from ..action_monitor.actions import Decision, Verdict
from ..code_risk.analyzer import ChangeRisk, RiskLevel
from ..norms.norms import NormFinding


class ReliabilityVerdict(str, Enum):
    """Sentinel's answer on a whole change."""

    SAFE = "SAFE"
    REVIEW = "REVIEW"
    CONDITIONAL = "CONDITIONAL"
    STOP = "STOP"


class CIStatus(str, Enum):
    """Whether the change's tests passed.

    UNKNOWN is a real, distinct state — it means we could not find out, which
    is not the same as passing.
    """

    PASSING = "passing"
    FAILING = "failing"
    PENDING = "pending"
    NONE = "none"
    UNKNOWN = "unknown"


_ORDER = {
    ReliabilityVerdict.SAFE: 0,
    ReliabilityVerdict.REVIEW: 1,
    ReliabilityVerdict.CONDITIONAL: 2,
    ReliabilityVerdict.STOP: 3,
}

_PLAIN_ENGLISH = {
    ReliabilityVerdict.SAFE: "Nothing concerning found. This looks fine to ship.",
    ReliabilityVerdict.REVIEW: "A human should look at this before it merges.",
    ReliabilityVerdict.CONDITIONAL: "This can ship, but only once the conditions below are met.",
    ReliabilityVerdict.STOP: "Do not ship this as it stands.",
}


def _bullet(text: str) -> str:
    """One report line, with any continuation lines lined up under it.

    Reasons come from several sources and some carry their own line breaks -
    an Ollama error that says how to start it, for instance. Without this they
    dedent to the left margin and stop looking like part of the bullet.
    """
    first, *rest = str(text).splitlines() or [""]
    return "\n".join([f"  - {first}"] + [f"    {line.strip()}" for line in rest])


def most_serious(verdicts: list[ReliabilityVerdict]) -> ReliabilityVerdict:
    """The worst verdict in the list. SAFE if the list is empty."""
    return max(verdicts, key=lambda verdict: _ORDER[verdict], default=ReliabilityVerdict.SAFE)


@dataclass(frozen=True)
class Assessment:
    """Sentinel's full answer: the verdict, why, and what it could not check."""

    verdict: ReliabilityVerdict
    reasons: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        return f"{self.verdict.value} - {_PLAIN_ENGLISH[self.verdict]}"

    def report(self) -> str:
        """The verdict as a human reads it. Plain text, ASCII only."""
        lines = [self.headline]

        if self.reasons:
            lines.append("")
            lines.append("Why:")
            lines += [_bullet(reason) for reason in self.reasons]

        if self.conditions:
            lines.append("")
            lines.append("Ship only once:")
            lines += [_bullet(condition) for condition in self.conditions]

        if self.unchecked:
            lines.append("")
            lines.append("Could not verify:")
            lines += [_bullet(item) for item in self.unchecked]
            lines.append("")
            lines.append("  An unverified check is not a pass.")

        return "\n".join(lines)


def _from_actions(decisions: list[Decision]) -> tuple[ReliabilityVerdict, list[str]]:
    """What the Action Monitor's findings alone imply."""
    blocked = [d for d in decisions if d.verdict is Verdict.BLOCKED]
    flagged = [d for d in decisions if d.verdict is Verdict.FLAGGED]
    reasons = []

    if blocked:
        reasons.append(
            f"the agent took {len(blocked)} action(s) it should not have: "
            + "; ".join(d.action.describe() for d in blocked[:3])
        )
    if flagged:
        reasons.append(
            f"{len(flagged)} action(s) need a human's eyes: "
            + "; ".join(d.action.describe() for d in flagged[:3])
        )

    if blocked:
        return ReliabilityVerdict.STOP, reasons
    if flagged:
        return ReliabilityVerdict.REVIEW, reasons
    return ReliabilityVerdict.SAFE, reasons


def _from_risk(risk: ChangeRisk | None) -> tuple[ReliabilityVerdict, list[str], list[str]]:
    """What the Code Risk Analyzer's score alone implies.

    HIGH risk becomes CONDITIONAL rather than STOP: touching payment code is a
    reason to demand review, not a reason to assume the change is broken. The
    specific findings become the conditions to satisfy.
    """
    if risk is None:
        return ReliabilityVerdict.SAFE, [], []

    if risk.level is RiskLevel.HIGH:
        conditions = [f"{f.path}: {'; '.join(f.reasons)}" for f in risk.notable_files[:5]]
        return (
            ReliabilityVerdict.CONDITIONAL,
            [f"the change scores HIGH risk across {len(risk.files)} file(s)"],
            conditions or ["a reviewer signs off on the high-risk files above"],
        )

    if risk.level is RiskLevel.MEDIUM:
        detail = risk.reasons or [f.describe() for f in risk.notable_files[:3]]
        return ReliabilityVerdict.REVIEW, [f"medium risk: {'; '.join(detail)}"], []

    return ReliabilityVerdict.SAFE, [], []


def _from_norms(findings: list[NormFinding]) -> tuple[ReliabilityVerdict, list[str]]:
    """What breaking the project's own rules implies.

    The severity of each norm was written by a human in the config, so this
    stays deterministic even for findings the model reported: the model only
    said *whether* a rule was broken, never how much it matters.
    """
    blocked = [f for f in findings if f.severity is Verdict.BLOCKED]
    flagged = [f for f in findings if f.severity is Verdict.FLAGGED]
    reasons = []

    if blocked:
        reasons.append(
            f"the change breaks {len(blocked)} rule(s) this project treats as blocking: "
            + "; ".join(f.describe() for f in blocked[:3])
        )
    if flagged:
        reasons.append(
            f"the change breaks {len(flagged)} of this project's rule(s): "
            + "; ".join(f.describe() for f in flagged[:3])
        )

    if blocked:
        return ReliabilityVerdict.STOP, reasons
    if flagged:
        return ReliabilityVerdict.REVIEW, reasons
    return ReliabilityVerdict.SAFE, reasons


def _from_ci(status: CIStatus) -> tuple[ReliabilityVerdict, list[str], list[str]]:
    """What CI alone implies. Returns (verdict, reasons, unchecked)."""
    if status is CIStatus.FAILING:
        return ReliabilityVerdict.STOP, ["CI is failing on this change"], []
    if status is CIStatus.PENDING:
        return ReliabilityVerdict.REVIEW, ["CI has not finished yet"], []
    if status is CIStatus.UNKNOWN:
        return ReliabilityVerdict.SAFE, [], ["CI status (could not be read)"]
    if status is CIStatus.NONE:
        return ReliabilityVerdict.SAFE, [], ["CI status (this repo has no checks configured)"]
    return ReliabilityVerdict.SAFE, ["CI is passing"], []


def synthesize(
    decisions: list[Decision] | None = None,
    risk: ChangeRisk | None = None,
    ci: CIStatus = CIStatus.UNKNOWN,
    unchecked: list[str] | None = None,
    norm_findings: list[NormFinding] | None = None,
) -> Assessment:
    """Combine every signal into one verdict.

    Pure: everything is passed in, nothing is fetched. `review.py` does the
    gathering, which keeps this testable against mock signals
    (instructions.md #5).

    `decisions=None` means the Action Monitor was not watching — recorded as
    unverified, never treated as "nothing happened".

    `norm_findings=None` means the project's norms were not checked, and is
    recorded the same way. `[]` means they were checked and nothing was broken.
    A caller for a project with no norms configured should pass `[]`: there is
    nothing to check, so nothing was missed.
    """
    gathered_unchecked = list(unchecked or [])

    if decisions is None:
        action_verdict, action_reasons = ReliabilityVerdict.SAFE, []
        gathered_unchecked.append("what the coding agent did (the Action Monitor was not watching)")
    else:
        action_verdict, action_reasons = _from_actions(decisions)

    if norm_findings is None:
        norm_verdict, norm_reasons = ReliabilityVerdict.SAFE, []
        gathered_unchecked.append("whether the change follows this project's own rules (they were not checked)")
    else:
        norm_verdict, norm_reasons = _from_norms(norm_findings)

    risk_verdict, risk_reasons, conditions = _from_risk(risk)
    ci_verdict, ci_reasons, ci_unchecked = _from_ci(ci)

    gathered_unchecked.extend(ci_unchecked)
    if risk is not None:
        gathered_unchecked.extend(risk.unchecked)

    verdict = most_serious([action_verdict, norm_verdict, risk_verdict, ci_verdict])
    reasons = action_reasons + norm_reasons + risk_reasons + ci_reasons

    # An unverified signal must never leave the verdict at SAFE: we would be
    # claiming everything is fine while knowing we did not look.
    if gathered_unchecked and verdict is ReliabilityVerdict.SAFE:
        verdict = ReliabilityVerdict.REVIEW
        reasons.append("some checks could not be run, so this cannot be called safe")

    if verdict is not ReliabilityVerdict.CONDITIONAL:
        conditions = []

    # SAFE is the one verdict where nothing went wrong, so nothing above has
    # explained it. Say what was actually checked - "SAFE" with no basis is
    # exactly the kind of unearned reassurance Sentinel exists to prevent.
    if verdict is ReliabilityVerdict.SAFE:
        basis = "nothing flagged by the Action Monitor and the change scores low risk"
        if norm_findings is not None:
            basis += ", and it breaks none of this project's rules"
        reasons = [basis] + reasons

    return Assessment(
        verdict=verdict,
        reasons=reasons,
        conditions=conditions,
        unchecked=gathered_unchecked,
    )
