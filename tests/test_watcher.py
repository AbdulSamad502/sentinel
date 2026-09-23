"""Tests for the Action Monitor's watcher.

The event-translation logic is tested directly with synthetic watchdog events,
which is fast and deterministic. One end-to-end test then runs a real observer
over a temp directory to prove the wiring actually works — that one polls
rather than sleeping a fixed amount, since filesystem event latency varies by
platform.

    python -m unittest tests.test_watcher -v
"""

import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchdog.events import (
    DirModifiedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)
from watchdog.observers import Observer

from sentinel.action_monitor.actions import ActionType, Verdict
from sentinel.action_monitor.config import MonitorConfig, load_config
from sentinel.action_monitor.rules import check
from sentinel.action_monitor.watcher import ActionReporter, format_decision, is_noise

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


class QuietTestCase(unittest.TestCase):
    """Base for tests that drive the reporter, which prints as it goes.

    Call `self.silence()` before triggering events so a full test run stays
    readable instead of interleaving verdict output with the results.
    """

    def silence(self) -> None:
        captured = redirect_stdout(io.StringIO())
        captured.__enter__()
        self.addCleanup(captured.__exit__, None, None, None)


class TestNoiseFiltering(unittest.TestCase):
    def test_machine_churn_is_ignored(self):
        for path in (".git/index", "__pycache__/mod.pyc", ".venv/lib/site.py", "node_modules/x/i.js"):
            with self.subTest(path=path):
                self.assertTrue(is_noise(path))

    def test_real_work_is_not_ignored(self):
        for path in ("src/main.py", ".env", "docs/readme.md"):
            with self.subTest(path=path):
                self.assertFalse(is_noise(path))

    def test_a_directory_merely_named_like_noise_elsewhere_still_matches(self):
        # Nested vendor directories are just as noisy as top-level ones.
        self.assertTrue(is_noise("frontend/node_modules/pkg/index.js"))


class TestEventTranslation(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.reporter = ActionReporter(root=self.root, config=MonitorConfig())

    def _path(self, relative: str) -> str:
        return str(self.root / relative)

    def test_create_modify_delete_map_to_action_types(self):
        cases = [
            (FileCreatedEvent(self._path("a.py")), ActionType.CREATE),
            (FileModifiedEvent(self._path("a.py")), ActionType.MODIFY),
            (FileDeletedEvent(self._path("a.py")), ActionType.DELETE),
        ]
        for event, expected in cases:
            with self.subTest(event=event.event_type):
                action = self.reporter.to_action(event)
                self.assertIsNotNone(action)
                self.assertIs(action.type, expected)
                self.assertEqual(action.path, "a.py")

    def test_a_move_reports_where_the_file_landed(self):
        event = FileMovedEvent(self._path("old.py"), self._path("src/new.py"))
        action = self.reporter.to_action(event)
        self.assertEqual(action.path, "src/new.py")

    def test_directory_events_are_dropped(self):
        self.assertIsNone(self.reporter.to_action(DirModifiedEvent(self._path("src"))))

    def test_paths_outside_the_watched_tree_are_dropped(self):
        other = Path(tempfile.mkdtemp()).resolve() / "stray.py"
        self.assertIsNone(self.reporter.to_action(FileCreatedEvent(str(other))))

    def test_noise_paths_are_dropped(self):
        self.assertIsNone(self.reporter.to_action(FileModifiedEvent(self._path(".git/index"))))

    def test_paths_are_reported_repo_relative_with_forward_slashes(self):
        action = self.reporter.to_action(FileCreatedEvent(self._path("src/app/main.py")))
        self.assertEqual(action.path, "src/app/main.py")


class TestDebounce(QuietTestCase):
    def test_repeated_saves_report_once(self):
        self.silence()
        root = Path(tempfile.mkdtemp()).resolve()
        reporter = ActionReporter(root=root, config=load_config(REPO_CONFIG))
        event = FileModifiedEvent(str(root / ".env"))

        for _ in range(5):
            reporter.on_any_event(event)

        # One logical save, one report — not five.
        self.assertEqual(len(reporter.flagged), 1)

    def test_different_files_are_not_debounced_against_each_other(self):
        self.silence()
        root = Path(tempfile.mkdtemp()).resolve()
        reporter = ActionReporter(root=root, config=load_config(REPO_CONFIG))

        reporter.on_any_event(FileModifiedEvent(str(root / ".env")))
        reporter.on_any_event(FileModifiedEvent(str(root / "requirements.txt")))

        self.assertEqual(len(reporter.flagged), 2)

    def test_a_create_followed_by_a_modify_reports_once(self):
        # One `echo > file` fires both events. Reporting both is noise.
        self.silence()
        root = Path(tempfile.mkdtemp()).resolve()
        reporter = ActionReporter(root=root, config=load_config(REPO_CONFIG))

        reporter.on_any_event(FileCreatedEvent(str(root / ".env")))
        reporter.on_any_event(FileModifiedEvent(str(root / ".env")))

        self.assertEqual(len(reporter.flagged), 1)

    def test_a_delete_is_never_debounced_away(self):
        # Losing a file moments after writing it is exactly what the human
        # needs to see, so DELETE must survive the debounce window.
        self.silence()
        root = Path(tempfile.mkdtemp()).resolve()
        reporter = ActionReporter(root=root, config=load_config(REPO_CONFIG))

        reporter.on_any_event(FileCreatedEvent(str(root / "src/main.py")))
        reporter.on_any_event(FileDeletedEvent(str(root / "src/main.py")))

        self.assertEqual(len(reporter.flagged), 1)
        self.assertIs(reporter.flagged[0].action.type, ActionType.DELETE)


class TestReporting(QuietTestCase):
    def setUp(self):
        self.silence()
        self.config = load_config(REPO_CONFIG)
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.reporter = ActionReporter(root=self.root, config=self.config)

    def test_only_non_allowed_decisions_are_kept(self):
        self.reporter.on_any_event(FileModifiedEvent(str(self.root / "src/fine.py")))
        self.assertEqual(self.reporter.flagged, [])

        self.reporter.on_any_event(FileModifiedEvent(str(self.root / ".env")))
        self.assertEqual(len(self.reporter.flagged), 1)

    def test_summary_when_nothing_happened(self):
        self.assertEqual(self.reporter.summarise(), "Nothing flagged.")

    def test_summary_counts_by_severity(self):
        self.reporter.on_any_event(FileModifiedEvent(str(self.root / ".env")))
        self.reporter.on_any_event(FileModifiedEvent(str(self.root / "requirements.txt")))

        summary = self.reporter.summarise()
        self.assertIn("1 blocked, 1 flagged", summary)

    def test_format_includes_every_reason(self):
        from sentinel.action_monitor.actions import Action

        decision = check(Action(type=ActionType.DELETE, path=".env"), self.config)
        formatted = format_decision(decision)
        self.assertIn("STOP", formatted)
        for reason in decision.reasons:
            self.assertIn(reason, formatted)

    def test_allowed_actions_format_on_one_line(self):
        from sentinel.action_monitor.actions import Action

        decision = check(Action(type=ActionType.MODIFY, path="src/main.py"), self.config)
        self.assertIs(decision.verdict, Verdict.ALLOWED)
        self.assertEqual(len(format_decision(decision).splitlines()), 1)


class TestEndToEnd(QuietTestCase):
    """Runs a real observer over a real directory."""

    def test_watcher_sees_a_real_file_write(self):
        self.silence()
        root = Path(tempfile.mkdtemp()).resolve()
        reporter = ActionReporter(root=root, config=load_config(REPO_CONFIG))

        observer = Observer()
        observer.schedule(reporter, str(root), recursive=True)
        observer.start()

        try:
            (root / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")

            # Poll rather than sleep a fixed amount — event latency differs by
            # platform, and a fixed sleep is how these tests get flaky.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not reporter.flagged:
                time.sleep(0.1)

            self.assertTrue(reporter.flagged, "watcher saw no events within 10s")
            self.assertIs(reporter.flagged[0].verdict, Verdict.BLOCKED)
            self.assertEqual(reporter.flagged[0].action.path, ".env")
        finally:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
