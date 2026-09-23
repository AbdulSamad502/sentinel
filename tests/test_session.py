"""Tests for the session log - the record that makes explanation possible.

Everything here runs against a real temporary directory, because the whole
point of this module is what it does to a filesystem. It stays fast: the trees
are three files deep.

The tests that matter most are the ones about what the session *could not*
capture. A snapshot that quietly skips a file, and a session that says it saw
everything, is how Sentinel would end up reporting a change that never happened.

    python -m unittest tests.test_session -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.action_monitor.tree import is_safe_relative, walk_files
from sentinel.session import SessionConfig, SessionLog, load_session_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


def modify(path: str) -> Action:
    return Action(type=ActionType.MODIFY, path=path)


class SessionTestCase(unittest.TestCase):
    """A real temp repo, cleaned up afterwards."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)

    def write(self, relative: str, content: str = "original\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def log(self, **overrides) -> SessionLog:
        return SessionLog(self.root, SessionConfig(**overrides) if overrides else None)

    def record(self, log: SessionLog, path: str, kind: ActionType = ActionType.MODIFY) -> None:
        action = Action(type=kind, path=path)
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))


class TestBaseline(SessionTestCase):
    def test_it_captures_source_files_as_they_were(self):
        self.write("src/app.py", "before\n")
        log = self.log()
        log.start()

        self.write("src/app.py", "after\n")
        self.assertEqual(log.before_text("src/app.py"), "before\n")
        self.assertEqual(log.current_text("src/app.py"), "after\n")

    def test_a_file_that_did_not_exist_has_no_before(self):
        """None means 'created', which must not be confused with 'skipped'."""
        log = self.log()
        log.start()
        self.write("src/new.py", "hello\n")

        self.assertIsNone(log.before_text("src/new.py"))
        self.assertIsNone(log.was_skipped("src/new.py"))

    def test_ignored_directories_are_never_walked(self):
        self.write("node_modules/left-pad/index.js", "x\n")
        self.write(".git/COMMIT_EDITMSG", "x\n")
        self.write("src/app.py", "x\n")

        found = {relative for _, relative in walk_files(self.root)}
        self.assertIn("src/app.py", found)
        self.assertNotIn("node_modules/left-pad/index.js", found)
        self.assertNotIn(".git/COMMIT_EDITMSG", found)

    def test_starting_again_clears_the_previous_session(self):
        self.write("src/app.py", "first\n")
        log = self.log()
        log.start()
        self.record(log, "src/app.py")
        self.assertEqual(len(log.recorded()), 1)

        log.start()
        self.assertEqual(log.recorded(), [])

    def test_the_session_lives_under_dot_sentinel_so_the_watcher_ignores_it(self):
        log = self.log()
        log.start()
        self.assertTrue(log.directory.is_relative_to(self.root / ".sentinel"))


class TestWhatCouldNotBeCaptured(SessionTestCase):
    """A skipped file is named, never silently dropped."""

    def test_a_file_over_the_limit_is_skipped_with_a_reason(self):
        self.write("src/huge.py", "x" * 500)
        log = self.log(max_snapshot_bytes=100)
        log.start()

        why = log.was_skipped("src/huge.py")
        self.assertIsNotNone(why)
        self.assertIn("100", why)

    def test_a_binary_file_is_skipped_with_a_reason(self):
        path = self.root / "src" / "blob.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"import x\x00\x00binary")

        log = self.log()
        log.start()
        self.assertIn("binary", log.was_skipped("src/blob.py") or "")

    def test_the_total_budget_stops_the_snapshot_and_says_so(self):
        for index in range(5):
            self.write(f"src/f{index}.py", "y" * 90)

        log = self.log(max_snapshot_bytes=1000, max_total_bytes=200)
        log.start()

        skipped = [entry["path"] for entry in log.skipped()]
        self.assertTrue(skipped, "some files should have hit the budget")
        self.assertIn("budget", log.was_skipped(skipped[0]) or "")

    def test_reading_an_oversized_current_file_is_a_problem_not_a_deletion(self):
        """Returning None for both would look like the agent deleted the file."""
        self.write("src/app.py", "small\n")
        log = self.log(max_snapshot_bytes=50)
        log.start()

        self.write("src/app.py", "z" * 400)
        self.assertIsNone(log.current_text("src/app.py"))
        self.assertTrue(any("src/app.py" in problem for problem in log.problems))

    def test_skipped_files_survive_into_a_second_process(self):
        """The explainer is a different process; it reads this off disk."""
        self.write("src/huge.py", "x" * 500)
        self.log(max_snapshot_bytes=100).start()

        reopened = SessionLog(self.root, SessionConfig(max_snapshot_bytes=100))
        self.assertIsNotNone(reopened.was_skipped("src/huge.py"))


class TestRecordingActions(SessionTestCase):
    def test_every_action_is_recorded_including_allowed_ones(self):
        """The watcher's `flagged` keeps only problems; this keeps everything."""
        log = self.log()
        log.start()
        self.record(log, "src/a.py")
        self.record(log, "src/b.py", ActionType.CREATE)

        self.assertEqual([entry.path for entry in log.recorded()], ["src/a.py", "src/b.py"])

    def test_repeated_saves_of_one_file_are_one_change(self):
        log = self.log()
        log.start()
        for _ in range(8):
            self.record(log, "src/a.py")

        self.assertEqual(len(log.recorded()), 1)

    def test_decisions_are_rebuilt_for_the_orchestrator(self):
        log = self.log()
        log.start()
        action = Action(type=ActionType.MODIFY, path=".env")
        log.record(action, Decision(action=action, verdict=Verdict.BLOCKED, reasons=["protected file"]))

        decisions = log.decisions()
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0].verdict, Verdict.BLOCKED)
        self.assertEqual(decisions[0].reasons, ["protected file"])

    def test_git_actions_are_not_recorded_as_file_changes(self):
        log = self.log()
        log.start()
        action = Action(type=ActionType.GIT, command="git push --force")
        log.record(action, Decision(action=action, verdict=Verdict.BLOCKED))

        self.assertEqual(log.recorded(), [])

    def test_a_half_written_line_does_not_lose_the_whole_log(self):
        """The watcher can be killed mid-append. The rest must still read."""
        log = self.log()
        log.start()
        self.record(log, "src/a.py")
        with log.actions_file.open("a", encoding="utf-8") as handle:
            handle.write('{"path": "src/b.py", "type": "mod')

        self.assertEqual([entry.path for entry in log.recorded()], ["src/a.py"])

    def test_recording_never_raises_even_when_the_log_cannot_be_written(self):
        """A failed write must not take the watcher down mid-session."""
        log = self.log()
        log.start()
        # The log file is created lazily on first append, so putting a
        # directory in its place makes every write fail.
        log.actions_file.mkdir(parents=True)

        self.record(log, "src/a.py")
        self.assertTrue(log.problems)
        self.assertEqual(log.recorded(), [])


class TestPathSafety(unittest.TestCase):
    def test_paths_that_climb_out_of_the_tree_are_refused(self):
        self.assertFalse(is_safe_relative("../outside.py"))
        self.assertFalse(is_safe_relative("a/../../b.py"))
        self.assertFalse(is_safe_relative("/etc/passwd"))
        self.assertFalse(is_safe_relative(""))

    def test_ordinary_relative_paths_are_fine(self):
        self.assertTrue(is_safe_relative("src/app.py"))
        self.assertTrue(is_safe_relative(".env"))
        self.assertTrue(is_safe_relative("a/b/c/d.py"))

    def test_a_climbing_path_gets_no_snapshot_location(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp))
            self.assertIsNone(log._snapshot_path("../escape.py"))
            self.assertIsNone(log.current_text("../escape.py"))


class TestSessionConfig(unittest.TestCase):
    def test_the_repos_own_config_loads(self):
        config = load_session_config(REPO_CONFIG)
        self.assertGreater(config.max_snapshot_bytes, 0)
        self.assertGreater(config.max_total_bytes, config.max_snapshot_bytes)

    def test_a_missing_section_means_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sentinel.config.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(load_session_config(path), SessionConfig())

    def test_meta_records_where_and_when(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            log = SessionLog(root)
            log.start()

            meta = json.loads(log.meta_file.read_text(encoding="utf-8"))
            self.assertEqual(meta["root"], str(root))
            self.assertIn("started", meta)


if __name__ == "__main__":
    unittest.main()
