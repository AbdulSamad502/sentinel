"""Tests for the timeline, doctor, markdown output and norm presets.

These four are about a person's experience of the tool rather than its
judgement, so what is pinned here is that they stay honest: the timeline keeps
its order, doctor never calls a warning a failure, markdown keeps "could not
verify" visible, and a preset can only ever select rules that actually exist.

    python -m unittest tests.test_tooling -v
"""

import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel import doctor
from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.explainer.explain import format_timeline, since_timestamp
from sentinel.explainer.diff import to_change
from sentinel.norms import catalog
from sentinel.norms.norms import BY_PATTERN, NormFinding
from sentinel.orchestrator.markdown import markdown_report
from sentinel.orchestrator.verdict import Assessment, ReliabilityVerdict
from sentinel.session import SessionLog

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


class TimelineTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)


class TestSince(unittest.TestCase):
    def test_relative_windows_are_what_people_type(self):
        self.assertEqual(since_timestamp("30m", NOW), "2026-08-18T11:30:00+00:00")
        self.assertEqual(since_timestamp("2h", NOW), "2026-08-18T10:00:00+00:00")
        self.assertEqual(since_timestamp("1d", NOW), "2026-08-17T12:00:00+00:00")

    def test_an_iso_timestamp_passes_straight_through(self):
        self.assertEqual(since_timestamp("2026-08-18T09:00:00+00:00", NOW), "2026-08-18T09:00:00+00:00")

    def test_nothing_means_no_filter(self):
        self.assertEqual(since_timestamp("", NOW), "")

    def test_junk_is_not_silently_treated_as_now(self):
        """Returning a real timestamp for nonsense would hide the whole session."""
        self.assertEqual(since_timestamp("banana", NOW), "banana")


class TestTimeline(TimelineTestCase):
    def record(self, log, path, kind=ActionType.MODIFY, verdict=Verdict.ALLOWED):
        action = Action(type=kind, path=path)
        log.record(action, Decision(action=action, verdict=verdict, reasons=["a reason"] if verdict is not Verdict.ALLOWED else []))

    def test_actions_and_consultations_appear_in_one_stream(self):
        log = SessionLog(self.root)
        log.start()
        self.record(log, "src/a.py")
        log.record_consultation("check_my_work")

        events = log.timeline()
        self.assertEqual(len(events), 2)
        self.assertEqual({event["kind"] for event in events}, {"file", "consultation"})

    def test_it_is_ordered_oldest_first(self):
        log = SessionLog(self.root)
        log.start()
        for name in ("a", "b", "c"):
            self.record(log, f"src/{name}.py")

        stamps = [event["at"] for event in log.timeline()]
        self.assertEqual(stamps, sorted(stamps))

    def test_since_drops_everything_before_it(self):
        log = SessionLog(self.root)
        log.start()
        self.record(log, "src/a.py")
        far_future = "2099-01-01T00:00:00+00:00"
        self.assertEqual(log.timeline(since=far_future), [])

    def test_a_git_command_shows_the_command_not_a_path(self):
        log = SessionLog(self.root)
        log.start()
        action = Action(type=ActionType.GIT, command="git push --force origin main")
        log.record(action, Decision(action=action, verdict=Verdict.BLOCKED, reasons=["destroys work"]))

        event = log.timeline()[0]
        self.assertEqual(event["kind"], "git")
        self.assertIn("push --force", event["what"])

    def test_an_empty_window_says_so_rather_than_printing_nothing(self):
        self.assertIn("Nothing recorded", format_timeline([]))

    def test_rendering_shows_the_clock_and_the_verdict_marker(self):
        events = [{"at": "2026-08-18T14:35:02+00:00", "kind": "file", "what": "modify .env",
                   "verdict": "BLOCKED", "reasons": ["protected file"]}]
        text = format_timeline(events)
        self.assertIn("14:35:02", text)
        self.assertIn("STOP", text)
        self.assertIn("protected file", text)
        text.encode("ascii")


class TestDoctor(TimelineTestCase):
    def test_a_missing_config_is_a_failure_with_a_fix(self):
        checks = doctor.run(self.root, self.root / "nope.json")
        config = next(check for check in checks if check.name == "config")
        self.assertEqual(config.status, doctor.FAIL)
        self.assertTrue(config.fix)

    def test_a_valid_config_passes_and_counts_the_rules(self):
        checks = doctor.check_config(self.root, REPO_CONFIG)
        self.assertEqual(checks[0].status, doctor.PASS)
        rules = next(check for check in checks if check.name == "rules")
        self.assertEqual(rules.status, doctor.PASS)
        self.assertIn("rule(s)", rules.detail)

    def test_no_session_is_a_warning_not_a_failure(self):
        """Sentinel works fine before you start watching."""
        check = doctor.check_session(self.root, REPO_CONFIG)
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("watcher", check.fix)

    def test_a_folder_without_git_warns_about_what_is_lost(self):
        check = doctor.check_hooks(self.root)
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("not a git repository", check.detail)

    def test_an_unreachable_model_is_never_a_failure(self):
        """The verdict, the rules and the diff all work without one."""
        self.assertNotEqual(doctor.check_model().status, doctor.FAIL)

    def test_every_non_passing_check_says_what_to_do(self):
        for check in doctor.run(self.root, REPO_CONFIG):
            if check.status != doctor.PASS:
                with self.subTest(check=check.name):
                    self.assertTrue(check.fix, f"{check.name} gives no remedy")

    def test_output_is_ascii_for_every_terminal(self):
        for check in doctor.run(self.root, REPO_CONFIG):
            check.describe().encode("ascii")


class TestMarkdownReport(unittest.TestCase):
    def assessment(self, verdict=ReliabilityVerdict.STOP) -> Assessment:
        return Assessment(
            verdict=verdict,
            reasons=["the agent modified .env"],
            conditions=[],
            unchecked=["CI status (could not be read)\nand a second line"],
        )

    def test_the_verdict_leads(self):
        text = markdown_report(self.assessment())
        self.assertTrue(text.startswith("## Sentinel: STOP"))

    def test_could_not_verify_is_present_and_stated_plainly(self):
        text = markdown_report(self.assessment())
        self.assertIn("Could not verify", text)
        self.assertIn("An unverified check is not a pass", text)

    def test_multi_line_reasons_collapse_so_the_list_does_not_break(self):
        """A bare newline ends a markdown list item."""
        text = markdown_report(self.assessment())
        for line in text.splitlines():
            if line.startswith("- CI status"):
                self.assertIn("and a second line", line)
                break
        else:
            self.fail("the unchecked entry was not rendered as one list item")

    def test_findings_carry_how_they_were_found(self):
        finding = NormFinding("no-models", "Model ids belong in config.", "a.py", Verdict.FLAGGED, 'M = "gpt-4o"', BY_PATTERN, 3)
        text = markdown_report(self.assessment(), findings=[finding])
        self.assertIn("no-models", text)
        self.assertIn("a.py:3", text)
        self.assertIn(BY_PATTERN, text)

    def test_a_pipe_in_evidence_cannot_break_the_table(self):
        finding = NormFinding("n", "Rule.", "a.py", Verdict.FLAGGED, "a | b | c", BY_PATTERN, 1)
        row = [line for line in markdown_report(self.assessment(), findings=[finding]).splitlines() if "a.py" in line][0]
        # Count only unescaped pipes: `\|` is a literal, not a column break.
        columns = len(re.findall(r"(?<!\\)\|", row))
        self.assertEqual(columns, 5, "an unescaped pipe added a column")
        self.assertIn(r"\|", row, "the pipe was not escaped at all")

    def test_changed_files_are_listed(self):
        changes = [to_change("src/a.py", "one\n", "two\n")]
        self.assertIn("src/a.py", markdown_report(self.assessment(), changes=changes))

    def test_it_says_sentinel_does_not_change_your_code(self):
        self.assertIn("does not change your code", markdown_report(self.assessment()))


class TestPresets(unittest.TestCase):
    def test_every_preset_names_only_rules_that_exist(self):
        """A typo here would silently produce a smaller rule set."""
        known = {norm.id for norm in catalog.all_norms()}
        for key, (_label, ids) in catalog.PRESETS.items():
            for norm_id in ids:
                with self.subTest(preset=key, norm=norm_id):
                    self.assertIn(norm_id, known)

    def test_every_preset_resolves_to_the_rules_it_names(self):
        for key, (_label, ids) in catalog.PRESETS.items():
            with self.subTest(preset=key):
                self.assertEqual(len(catalog.preset_norms(key)), len(set(ids)))

    def test_an_unknown_preset_is_empty_rather_than_an_error(self):
        self.assertEqual(catalog.preset_norms("not-a-preset"), [])

    def test_every_preset_guards_credentials(self):
        """Whatever the project is, committing a key is never acceptable."""
        for key in catalog.PRESETS:
            with self.subTest(preset=key):
                self.assertIn("no-hardcoded-credentials", {norm.id for norm in catalog.preset_norms(key)})

    def test_presets_have_readable_labels(self):
        for key, (label, _ids) in catalog.PRESETS.items():
            with self.subTest(preset=key):
                self.assertGreater(len(label.split()), 2)


if __name__ == "__main__":
    unittest.main()
