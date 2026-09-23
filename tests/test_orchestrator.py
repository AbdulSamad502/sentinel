"""Tests for the verdict synthesis and signal gathering.

All offline. The verdict rules are pure, so every rung of the ladder is tested
against mock signals; the gathering is tested with a stubbed GitHub client to
prove that one failing signal does not take the others down with it.

    python -m unittest tests.test_orchestrator -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.code_risk.analyzer import ChangeRisk, FileRisk, RiskLevel
from sentinel.norms.norms import NormFinding
from sentinel.orchestrator.review import interpret_checks, review_pull_request
from sentinel.orchestrator.verdict import (
    Assessment,
    CIStatus,
    ReliabilityVerdict,
    most_serious,
    synthesize,
)
from sentinel.shared.github import ChangedFile, GitHubError


def decision(verdict: Verdict, path: str = "src/main.py") -> Decision:
    return Decision(
        action=Action(type=ActionType.MODIFY, path=path),
        verdict=verdict,
        reasons=["test reason"] if verdict is not Verdict.ALLOWED else [],
    )


def norm_finding(severity: Verdict, source: str = "pattern") -> NormFinding:
    return NormFinding(
        norm_id="no-hardcoded-model-ids",
        statement="Model ids belong in configuration.",
        path="src/app.py",
        severity=severity,
        evidence='MODEL = "gpt-4o"',
        source=source,
        line=7,
    )


def risk_at(level: RiskLevel, unchecked: list[str] | None = None) -> ChangeRisk:
    files = [FileRisk(path="src/payments/charge.py", level=level, reasons=["touches payments code"])]
    return ChangeRisk(
        level=level,
        files=files,
        reasons=["a stated reason"] if level is not RiskLevel.LOW else [],
        unchecked=unchecked or [],
    )


# A clean baseline: nothing flagged, low risk, CI green, no norms broken. Used
# so each test can vary one signal and show that signal is what moved the
# verdict. Note `norm_findings=[]`, not None: [] means "checked, nothing
# broken", None means "not checked", and only the first can be part of a SAFE.
CLEAN = {
    "decisions": [],
    "risk": risk_at(RiskLevel.LOW),
    "ci": CIStatus.PASSING,
    "norm_findings": [],
}


class TestTheLadder(unittest.TestCase):
    def test_everything_clean_is_safe(self):
        self.assertIs(synthesize(**CLEAN).verdict, ReliabilityVerdict.SAFE)

    def test_a_blocked_action_stops_the_change(self):
        result = synthesize(**{**CLEAN, "decisions": [decision(Verdict.BLOCKED, ".env")]})
        self.assertIs(result.verdict, ReliabilityVerdict.STOP)
        self.assertIn("should not have", result.report())

    def test_a_flagged_action_needs_review(self):
        result = synthesize(**{**CLEAN, "decisions": [decision(Verdict.FLAGGED)]})
        self.assertIs(result.verdict, ReliabilityVerdict.REVIEW)

    def test_failing_ci_stops_the_change(self):
        result = synthesize(**{**CLEAN, "ci": CIStatus.FAILING})
        self.assertIs(result.verdict, ReliabilityVerdict.STOP)
        self.assertIn("CI is failing", result.report())

    def test_pending_ci_needs_review(self):
        self.assertIs(
            synthesize(**{**CLEAN, "ci": CIStatus.PENDING}).verdict, ReliabilityVerdict.REVIEW
        )

    def test_high_risk_is_conditional_not_stop(self):
        # Touching payment code is a reason to demand review, not a reason to
        # assume the change is broken.
        result = synthesize(**{**CLEAN, "risk": risk_at(RiskLevel.HIGH)})
        self.assertIs(result.verdict, ReliabilityVerdict.CONDITIONAL)
        self.assertTrue(result.conditions)

    def test_medium_risk_needs_review(self):
        self.assertIs(
            synthesize(**{**CLEAN, "risk": risk_at(RiskLevel.MEDIUM)}).verdict,
            ReliabilityVerdict.REVIEW,
        )

    def test_the_worst_signal_decides(self):
        # High risk alone is CONDITIONAL; a blocked action alongside it is STOP.
        result = synthesize(
            decisions=[decision(Verdict.BLOCKED)], risk=risk_at(RiskLevel.HIGH), ci=CIStatus.PASSING
        )
        self.assertIs(result.verdict, ReliabilityVerdict.STOP)

    def test_conditions_are_dropped_when_the_verdict_is_not_conditional(self):
        # Listing "ship once X" under a STOP would be contradictory advice.
        result = synthesize(
            decisions=[decision(Verdict.BLOCKED)], risk=risk_at(RiskLevel.HIGH), ci=CIStatus.PASSING
        )
        self.assertEqual(result.conditions, [])


class TestProjectNormsOnTheLadder(unittest.TestCase):
    """A project's own rules are a signal like any other, and rank the same way."""

    def test_a_blocking_norm_stops_the_change(self):
        result = synthesize(**{**CLEAN, "norm_findings": [norm_finding(Verdict.BLOCKED)]})
        self.assertIs(result.verdict, ReliabilityVerdict.STOP)
        self.assertIn("treats as blocking", result.report())

    def test_a_flagged_norm_asks_for_review(self):
        result = synthesize(**{**CLEAN, "norm_findings": [norm_finding(Verdict.FLAGGED)]})
        self.assertIs(result.verdict, ReliabilityVerdict.REVIEW)

    def test_the_report_names_the_rule_the_file_and_who_found_it(self):
        report = synthesize(**{**CLEAN, "norm_findings": [norm_finding(Verdict.FLAGGED)]}).report()
        self.assertIn("no-hardcoded-model-ids", report)
        self.assertIn("src/app.py", report)
        self.assertIn("pattern", report)

    def test_unchecked_norms_prevent_safe(self):
        # The project has rules and nobody looked at them. That is not a pass.
        result = synthesize(decisions=[], risk=risk_at(RiskLevel.LOW), ci=CIStatus.PASSING, norm_findings=None)
        self.assertIs(result.verdict, ReliabilityVerdict.REVIEW)
        self.assertIn("this project's own rules", result.report())

    def test_no_findings_is_different_from_not_checked(self):
        """[] means checked and clean; None means we do not know."""
        self.assertIs(synthesize(**CLEAN).verdict, ReliabilityVerdict.SAFE)

    def test_a_safe_verdict_says_the_rules_were_checked(self):
        self.assertIn("breaks none of this project's rules", synthesize(**CLEAN).report())

    def test_a_model_found_violation_ranks_by_the_configured_severity(self):
        """The model reports the break; the human's config sets what it costs."""
        result = synthesize(**{**CLEAN, "norm_findings": [norm_finding(Verdict.BLOCKED, source="model")]})
        self.assertIs(result.verdict, ReliabilityVerdict.STOP)
        self.assertIn("found by model", result.report())

    def test_the_same_findings_always_give_the_same_verdict(self):
        findings = [norm_finding(Verdict.FLAGGED), norm_finding(Verdict.BLOCKED)]
        first = synthesize(**{**CLEAN, "norm_findings": findings})
        second = synthesize(**{**CLEAN, "norm_findings": list(findings)})
        self.assertEqual(first.report(), second.report())


class TestUnverifiedIsNotAPass(unittest.TestCase):
    """The rule that keeps silence from reading as safety."""

    def test_unknown_ci_prevents_safe(self):
        result = synthesize(decisions=[], risk=risk_at(RiskLevel.LOW), ci=CIStatus.UNKNOWN)
        self.assertIs(result.verdict, ReliabilityVerdict.REVIEW)
        self.assertIn("could not", result.report().lower())

    def test_no_action_monitor_data_prevents_safe(self):
        # decisions=None means nobody was watching, which is not the same as
        # "the agent did nothing wrong".
        result = synthesize(decisions=None, risk=risk_at(RiskLevel.LOW), ci=CIStatus.PASSING)
        self.assertIs(result.verdict, ReliabilityVerdict.REVIEW)
        self.assertIn("was not watching", result.report())

    def test_empty_decisions_is_different_from_none(self):
        # An empty list means the monitor watched and saw nothing wrong.
        self.assertIs(synthesize(**CLEAN).verdict, ReliabilityVerdict.SAFE)

    def test_a_repo_with_no_ci_is_recorded_as_unverified(self):
        result = synthesize(decisions=[], risk=risk_at(RiskLevel.LOW), ci=CIStatus.NONE)
        self.assertIs(result.verdict, ReliabilityVerdict.REVIEW)
        self.assertIn("no checks configured", result.report())

    def test_risk_analyzer_gaps_are_carried_through(self):
        result = synthesize(
            decisions=[], risk=risk_at(RiskLevel.LOW, unchecked=["churn history"]), ci=CIStatus.PASSING
        )
        self.assertIn("churn history", result.report())

    def test_unverified_does_not_downgrade_a_worse_verdict(self):
        result = synthesize(decisions=None, risk=risk_at(RiskLevel.HIGH), ci=CIStatus.UNKNOWN)
        self.assertIs(result.verdict, ReliabilityVerdict.CONDITIONAL)


class TestReportText(unittest.TestCase):
    def test_report_is_ascii_for_the_windows_console(self):
        result = synthesize(decisions=[decision(Verdict.BLOCKED)], risk=risk_at(RiskLevel.HIGH))
        result.report().encode("ascii")  # raises if a non-ASCII character crept in

    def test_safe_report_states_what_was_actually_checked(self):
        # "SAFE" with no stated basis is exactly the unearned reassurance
        # Sentinel exists to prevent.
        report = synthesize(**CLEAN).report()
        self.assertIn("nothing flagged", report)
        self.assertIn("low risk", report)
        self.assertIn("CI is passing", report)

    def test_report_warns_that_unverified_is_not_a_pass(self):
        result = synthesize(decisions=None, risk=risk_at(RiskLevel.LOW), ci=CIStatus.PASSING)
        self.assertIn("not a pass", result.report())

    def test_every_verdict_has_a_plain_english_headline(self):
        for verdict in ReliabilityVerdict:
            with self.subTest(verdict=verdict):
                headline = Assessment(verdict=verdict).headline
                self.assertIn(verdict.value, headline)
                self.assertGreater(len(headline), len(verdict.value) + 10)


class TestCIInterpretation(unittest.TestCase):
    def test_no_checks_means_none(self):
        self.assertIs(interpret_checks([]), CIStatus.NONE)

    def test_all_successful_means_passing(self):
        self.assertIs(interpret_checks(["success", "skipped", "neutral"]), CIStatus.PASSING)

    def test_one_failure_fails_the_change(self):
        self.assertIs(interpret_checks(["success", "failure", "success"]), CIStatus.FAILING)

    def test_a_timeout_counts_as_failure(self):
        self.assertIs(interpret_checks(["success", "timed_out"]), CIStatus.FAILING)

    def test_an_unfinished_run_means_pending(self):
        self.assertIs(interpret_checks(["success", "in_progress"]), CIStatus.PENDING)

    def test_failure_beats_pending(self):
        self.assertIs(interpret_checks(["in_progress", "failure"]), CIStatus.FAILING)


class StubClient:
    """A GitHub client that fails exactly where a test tells it to."""

    def __init__(self, fail_on: set[str] | None = None, conclusions: list[str] | None = None):
        self.fail_on = fail_on or set()
        self.conclusions = conclusions if conclusions is not None else ["success"]
        self.is_authenticated = True

    def _maybe_fail(self, name: str):
        if name in self.fail_on:
            raise GitHubError(f"{name} unavailable")

    def pull_request_files(self, owner, repo, number):
        self._maybe_fail("files")
        return [ChangedFile(path="src/main.py", status="modified", additions=5, deletions=1)]

    def commit_count_for_path(self, owner, repo, path, limit=30):
        self._maybe_fail("churn")
        return 1

    def pull_request_head_sha(self, owner, repo, number):
        self._maybe_fail("head")
        return "abc123"

    def check_conclusions(self, owner, repo, ref):
        self._maybe_fail("checks")
        return self.conclusions


class TestSignalIsolation(unittest.TestCase):
    """One broken signal must not take the others down (instructions.md #7)."""

    def test_a_working_review_produces_a_verdict(self):
        result = review_pull_request(StubClient(), "o", "r", 1)
        self.assertIsInstance(result, Assessment)

    def test_ci_failure_to_load_still_leaves_the_risk_score(self):
        result = review_pull_request(StubClient(fail_on={"head", "checks"}), "o", "r", 1)
        self.assertIn("CI status", result.report())
        # The risk analysis still ran: the missing-test finding survives.
        self.assertIn("test", result.report().lower())

    def test_risk_failure_still_leaves_ci(self):
        result = review_pull_request(StubClient(fail_on={"files"}), "o", "r", 1)
        self.assertIn("code risk analysis", result.report())
        self.assertIsInstance(result, Assessment)

    def test_everything_failing_still_returns_a_verdict_not_an_exception(self):
        result = review_pull_request(StubClient(fail_on={"files", "head", "checks"}), "o", "r", 1)
        self.assertIs(result.verdict, ReliabilityVerdict.REVIEW)
        self.assertGreaterEqual(len(result.unchecked), 2)

    def test_a_failing_build_reaches_the_verdict(self):
        result = review_pull_request(StubClient(conclusions=["failure"]), "o", "r", 1)
        self.assertIs(result.verdict, ReliabilityVerdict.STOP)


class TestOrdering(unittest.TestCase):
    def test_most_serious(self):
        self.assertIs(
            most_serious([ReliabilityVerdict.SAFE, ReliabilityVerdict.STOP, ReliabilityVerdict.REVIEW]),
            ReliabilityVerdict.STOP,
        )
        self.assertIs(
            most_serious([ReliabilityVerdict.REVIEW, ReliabilityVerdict.CONDITIONAL]),
            ReliabilityVerdict.CONDITIONAL,
        )
        self.assertIs(most_serious([]), ReliabilityVerdict.SAFE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
