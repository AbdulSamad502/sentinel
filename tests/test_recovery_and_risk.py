"""Tests for recovery hints and Code Risk on local sessions.

Recovery is the one place Sentinel comes closest to acting on a codebase, so
the tests pin the boundary: it produces a *command*, and running it is the
human's business. One test actually executes the offered command and checks the
file comes back, because a recovery hint that does not work is worse than none.

Code Risk previously ran on GitHub pull requests only, even though local
sessions carry real diffs. These tests pin that a local session is now scored
on the same four signals.

    python -m unittest tests.test_recovery_and_risk -v
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.offline import go_offline

from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.code_risk.churn import is_git_repo, local_churn
from sentinel.explainer.diff import build_changes, recoveries, recovery_report
from sentinel.orchestrator.review import as_changed_files, review_session
from sentinel.orchestrator.verdict import ReliabilityVerdict
from sentinel.session import SessionLog

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


class LocalRepoTestCase(unittest.TestCase):
    def setUp(self):
        # review_session() would otherwise call a real model whenever one
        # happens to be running. See tests/offline.py.
        go_offline(self)
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)
        (self.root / "sentinel.config.json").write_text(REPO_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def record(self, log: SessionLog, path: str, kind: ActionType) -> None:
        action = Action(type=kind, path=path)
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))


class TestRecoveryHints(LocalRepoTestCase):
    def test_a_deleted_file_is_offered_back(self):
        self.write("src/legacy.py", "def old():\n    return 1\n")
        log = SessionLog(self.root)
        log.start()

        (self.root / "src" / "legacy.py").unlink()
        self.record(log, "src/legacy.py", ActionType.DELETE)

        changes, _ = build_changes(log)
        found = recoveries(log, changes)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, "src/legacy.py")

    def test_the_offered_command_actually_restores_the_file(self):
        """A recovery hint that does not work is worse than none at all."""
        self.write("src/legacy.py", "def old():\n    return 1\n")
        log = SessionLog(self.root)
        log.start()

        target = self.root / "src" / "legacy.py"
        target.unlink()
        self.record(log, "src/legacy.py", ActionType.DELETE)

        changes, _ = build_changes(log)
        command = recoveries(log, changes)[0].command()

        # Run it from somewhere else entirely: both paths must be absolute, or
        # the restore lands in whatever directory the terminal happened to be in.
        subprocess.run(command, shell=True, cwd=tempfile.gettempdir(), check=True)

        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "def old():\n    return 1\n")

    def test_a_modified_file_is_not_offered_for_recovery(self):
        self.write("src/app.py", "one\n")
        log = SessionLog(self.root)
        log.start()
        self.write("src/app.py", "two\n")
        self.record(log, "src/app.py", ActionType.MODIFY)

        changes, _ = build_changes(log)
        self.assertEqual(recoveries(log, changes), [])

    def test_a_file_created_and_deleted_has_nothing_to_restore(self):
        log = SessionLog(self.root)
        log.start()
        self.write("src/temp.py", "scratch\n")
        self.record(log, "src/temp.py", ActionType.CREATE)
        (self.root / "src" / "temp.py").unlink()
        self.record(log, "src/temp.py", ActionType.DELETE)

        changes, _ = build_changes(log)
        self.assertEqual(recoveries(log, changes), [])

    def test_the_report_says_sentinel_will_not_run_it(self):
        """The boundary: Sentinel hands over a command, it does not act."""
        self.write("src/legacy.py", "x\n")
        log = SessionLog(self.root)
        log.start()
        (self.root / "src" / "legacy.py").unlink()
        self.record(log, "src/legacy.py", ActionType.DELETE)

        changes, _ = build_changes(log)
        text = recovery_report(recoveries(log, changes))
        self.assertIn("will not run these for you", text)
        text.encode("ascii")

    def test_nothing_deleted_produces_no_report(self):
        self.assertEqual(recovery_report([]), "")


class TestTranslatingChangesForTheRiskAnalyzer(unittest.TestCase):
    def test_statuses_map_to_githubs_vocabulary(self):
        """`assess_file` tests `status == "removed"` directly."""
        from sentinel.explainer.diff import to_change

        files = as_changed_files(
            [
                to_change("a.py", None, "new\n"),
                to_change("b.py", "old\n", None),
                to_change("c.py", "one\n", "two\n"),
            ]
        )
        self.assertEqual([f.status for f in files], ["added", "removed", "modified"])

    def test_line_counts_carry_over(self):
        from sentinel.explainer.diff import to_change

        files = as_changed_files([to_change("a.py", "one\n", "one\ntwo\nthree\n")])
        self.assertEqual(files[0].additions, 2)
        self.assertEqual(files[0].deletions, 0)


class TestLocalChurn(LocalRepoTestCase):
    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)

    def test_a_folder_without_git_says_so_rather_than_guessing(self):
        counts, unchecked = local_churn(self.root, ["src/app.py"], lookback=30)
        self.assertEqual(counts, {})
        self.assertTrue(any("not a git repository" in item for item in unchecked))
        self.assertFalse(is_git_repo(self.root))

    def test_it_counts_commits_that_touched_the_file(self):
        self.git("init", "-q")
        self.git("config", "user.email", "t@t.t")
        self.git("config", "user.name", "T")
        self.write("src/app.py", "one\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "first")
        for number in range(3):
            self.write("src/app.py", f"rev {number}\n")
            self.git("commit", "-qam", f"rev {number}")

        counts, unchecked = local_churn(self.root, ["src/app.py"], lookback=30)
        self.assertEqual(counts["src/app.py"], 4)
        self.assertEqual(unchecked, [])

    def test_no_paths_means_nothing_to_look_up(self):
        self.assertEqual(local_churn(self.root, [], lookback=30), ({}, []))


class TestCodeRiskReachesTheLocalVerdict(LocalRepoTestCase):
    def test_touching_sensitive_code_now_moves_the_local_verdict(self):
        """Before this, a local session had no risk signal at all."""
        self.write("src/payments/charge.py", "def charge(amount):\n    return amount\n")
        log = SessionLog(self.root)
        log.start()
        self.write("src/payments/charge.py", "def charge(amount, currency):\n    return amount * rate(currency)\n")
        self.record(log, "src/payments/charge.py", ActionType.MODIFY)

        assessment, _changes, _findings = review_session(self.root)

        self.assertIs(assessment.verdict, ReliabilityVerdict.CONDITIONAL)
        self.assertIn("payments", assessment.report())

    def test_an_ordinary_change_does_not_score_high(self):
        self.write("docs/notes.md", "one\n")
        log = SessionLog(self.root)
        log.start()
        self.write("docs/notes.md", "one\ntwo\n")
        self.record(log, "docs/notes.md", ActionType.MODIFY)

        assessment, _changes, _findings = review_session(self.root)
        self.assertIsNot(assessment.verdict, ReliabilityVerdict.CONDITIONAL)

    def test_a_session_with_no_changes_scores_no_risk(self):
        SessionLog(self.root).start()
        assessment, changes, _findings = review_session(self.root)
        self.assertEqual(changes, [])
        self.assertIsNot(assessment.verdict, ReliabilityVerdict.CONDITIONAL)


if __name__ == "__main__":
    unittest.main()
