"""Tests for the Change Explainer.

The deterministic half is tested directly: given a before and an after, does
Sentinel describe the right change, with the right line numbers, and say so
when it could not read something.

The model half is tested only for what happens when the model is unavailable,
because that is the behaviour the design leans on - the summary is always
correct, and narration is an improvement on it, never a replacement.

    python -m unittest tests.test_explainer -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.explainer import explain as explain_module
from sentinel.explainer.config import ExplainConfig, load_explain_config
from sentinel.explainer.diff import (
    ADDED,
    DELETED,
    MODIFIED,
    build_changes,
    numbered_additions,
    summarise,
    to_change,
    truncate_diff,
)
from sentinel.session import SessionConfig, SessionLog

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


class TestDescribingOneFile(unittest.TestCase):
    def test_a_new_file_is_added(self):
        change = to_change("src/new.py", None, "line one\nline two\n")
        self.assertEqual(change.status, ADDED)
        self.assertEqual(len(change.added_lines), 2)
        self.assertEqual(change.removed_lines, [])

    def test_a_removed_file_is_deleted(self):
        change = to_change("src/old.py", "gone\n", None)
        self.assertEqual(change.status, DELETED)
        self.assertEqual(len(change.removed_lines), 1)

    def test_an_edited_file_is_modified_with_both_counts(self):
        change = to_change("src/a.py", "one\ntwo\n", "one\ntwo changed\nthree\n")
        self.assertEqual(change.status, MODIFIED)
        self.assertEqual(len(change.added_lines), 2)
        self.assertEqual(len(change.removed_lines), 1)

    def test_the_file_headers_are_not_counted_as_changed_lines(self):
        """`---`/`+++` start with - and + but are not content."""
        change = to_change("src/a.py", None, "only\n")
        self.assertEqual(change.added_lines, ["only"])

    def test_describe_is_ascii_for_the_windows_console(self):
        to_change("src/a.py", "a\n", "b\n").describe().encode("ascii")


class TestLineNumbers(unittest.TestCase):
    """A norm violation reported at the wrong line is worse than no line."""

    def test_added_lines_carry_their_position_in_the_new_file(self):
        change = to_change("a.py", "one\ntwo\nthree\n", "one\ntwo\nINSERTED\nthree\n")
        self.assertEqual(numbered_additions(change), [(3, "INSERTED")])

    def test_a_new_file_numbers_from_one(self):
        change = to_change("a.py", None, "first\nsecond\n")
        self.assertEqual(numbered_additions(change), [(1, "first"), (2, "second")])

    def test_a_repeated_line_is_reported_where_it_was_added(self):
        """Searching the file for the text would find the first copy instead."""
        change = to_change("a.py", "dup\nb\n", "dup\nb\ndup\n")
        self.assertEqual(numbered_additions(change), [(3, "dup")])

    def test_a_deleted_file_has_no_additions(self):
        self.assertEqual(numbered_additions(to_change("a.py", "x\n", None)), [])


class TestSummary(unittest.TestCase):
    def test_it_totals_the_change_and_lists_every_file(self):
        changes = [
            to_change("src/a.py", "one\n", "one\ntwo\n"),
            to_change("src/gone.py", "x\n", None),
        ]
        text = summarise(changes)
        self.assertIn("2 file(s) changed", text)
        self.assertIn("src/a.py", text)
        self.assertIn("deleted", text)

    def test_an_empty_session_says_so_rather_than_showing_nothing(self):
        self.assertIn("No file changes", summarise([]))

    def test_what_could_not_be_read_is_stated(self):
        text = summarise([], ["src/blob.bin (looks like a binary file)"])
        self.assertIn("Could not read", text)
        self.assertIn("blob.bin", text)

    def test_it_is_ascii_for_the_windows_console(self):
        summarise([to_change("a.py", "x\n", "y\n")], ["something"]).encode("ascii")


class TestTruncation(unittest.TestCase):
    def test_a_short_diff_is_untouched(self):
        self.assertEqual(truncate_diff("a\nb", 10), "a\nb")

    def test_a_long_diff_says_how_much_was_hidden(self):
        """A silently truncated diff makes the model describe a partial change."""
        result = truncate_diff("\n".join(str(n) for n in range(20)), 5)
        self.assertIn("15 more diff line(s) not shown", result)
        self.assertEqual(len(result.splitlines()), 6)


class TestBuildingFromASession(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def record(self, log: SessionLog, path: str, kind: ActionType = ActionType.MODIFY) -> None:
        action = Action(type=kind, path=path)
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))

    def test_it_reconstructs_the_three_kinds_of_change(self):
        self.write("src/edit.py", "before\n")
        self.write("src/gone.py", "doomed\n")
        log = SessionLog(self.root)
        log.start()

        self.write("src/edit.py", "after\n")
        (self.root / "src" / "gone.py").unlink()
        self.write("src/new.py", "fresh\n")
        for path in ["src/edit.py", "src/gone.py", "src/new.py"]:
            self.record(log, path)

        changes, unchecked = build_changes(log)
        by_path = {change.path: change.status for change in changes}
        self.assertEqual(by_path, {"src/edit.py": MODIFIED, "src/gone.py": DELETED, "src/new.py": ADDED})
        self.assertEqual(unchecked, [])

    def test_a_file_touched_but_not_changed_is_not_reported(self):
        """A formatter rewriting identical bytes is not a change."""
        self.write("src/a.py", "same\n")
        log = SessionLog(self.root)
        log.start()

        self.write("src/a.py", "same\n")
        self.record(log, "src/a.py")

        changes, _ = build_changes(log)
        self.assertEqual(changes, [])

    def test_a_file_created_then_removed_leaves_no_net_change(self):
        log = SessionLog(self.root)
        log.start()

        self.write("src/temp.py", "scratch\n")
        self.record(log, "src/temp.py", ActionType.CREATE)
        (self.root / "src" / "temp.py").unlink()
        self.record(log, "src/temp.py", ActionType.DELETE)

        changes, _ = build_changes(log)
        self.assertEqual(changes, [])

    def test_a_file_the_baseline_skipped_is_unchecked_not_missing(self):
        self.write("src/huge.py", "x" * 500)
        log = SessionLog(self.root, SessionConfig(max_snapshot_bytes=100))
        log.start()

        self.write("src/huge.py", "y" * 500)
        self.record(log, "src/huge.py")

        changes, unchecked = build_changes(log)
        self.assertEqual(changes, [])
        self.assertTrue(any("src/huge.py" in item for item in unchecked))


class TestNarrationDegradesToTheSummary(unittest.TestCase):
    """The verdict of this whole design: a model failure costs nothing."""

    def test_an_empty_session_needs_no_model_at_all(self):
        self.assertIn("No file changes", explain_module.explain_session([], []))

    def test_a_failure_while_building_the_prompt_still_returns_the_summary(self):
        changes = [to_change("src/a.py", "one\n", "two\n")]
        original = explain_module.session_prompt
        explain_module.session_prompt = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = explain_module.explain_session(changes, [])
        finally:
            explain_module.session_prompt = original

        self.assertIn("src/a.py", result)
        self.assertIn("1 file(s) changed", result)

    def test_the_prompt_carries_the_summary_and_the_diff(self):
        changes = [to_change("src/a.py", "one\n", "two\n")]
        prompt = explain_module.session_prompt(changes, [], ExplainConfig())
        self.assertIn("src/a.py", prompt)
        self.assertIn("+two", prompt)

    def test_the_prompt_says_when_files_were_left_out(self):
        changes = [to_change(f"src/f{n}.py", "a\n", "b\n") for n in range(5)]
        prompt = explain_module.session_prompt(changes, [], ExplainConfig(max_files=2))
        self.assertIn("3 more changed file(s) were not shown", prompt)


class TestExplainConfig(unittest.TestCase):
    def test_the_repos_own_config_loads(self):
        config = load_explain_config(REPO_CONFIG)
        self.assertGreater(config.max_diff_lines, 0)
        self.assertGreater(config.max_files, 0)

    def test_a_missing_section_means_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sentinel.config.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(load_explain_config(path), ExplainConfig())


if __name__ == "__main__":
    unittest.main()
