"""Tests for git command capture.

These close the gap Phase 1 left open and documented: the rules understood
dangerous git commands but nothing fed real ones in.

The load-bearing test in this file is `test_every_hook_exits_zero_whatever_the_verdict`.
A pre-push hook that exits non-zero aborts the push, and Sentinel does not get
to do that - it reports, and the human decides (instructions.md #6). Everything
else here can be wrong and be fixed; that one being wrong makes Sentinel
something it has promised not to be.

    python -m unittest tests.test_githooks -v
"""

import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.action_monitor.actions import ActionType, Verdict
from sentinel.action_monitor.githooks import (
    HOOKS,
    hook_script,
    install,
    is_ours,
    normalise,
    record_command,
    uninstall,
)
from sentinel.session import SessionLog
from sentinel.shared.config_file import ConfigError

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


class TestNormalisingTheCommand(unittest.TestCase):
    """`ps` reports a resolved path; the config's patterns are written as typed."""

    def test_a_resolved_git_path_becomes_plain_git(self):
        self.assertEqual(normalise("/opt/homebrew/bin/git push --force origin main"), "git push --force origin main")

    def test_an_already_plain_command_is_unchanged(self):
        self.assertEqual(normalise("git reset --hard"), "git reset --hard")

    def test_repeated_whitespace_collapses(self):
        self.assertEqual(normalise("git   push    --force"), "git push --force")

    def test_a_windows_git_exe_is_recognised(self):
        """os.path.basename would not split this on macOS or Linux."""
        self.assertEqual(normalise(r"C:\Program Files\Git\bin\git.exe push -f"), "git push -f")

    def test_something_that_is_not_git_survives_rather_than_vanishing(self):
        self.assertEqual(normalise("  weird  input "), "weird input")


class GitRepoTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        (self.root / ".git" / "hooks").mkdir(parents=True)
        (self.root / "sentinel.config.json").write_text(REPO_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        self.addCleanup(self._temp.cleanup)

    @property
    def hooks(self) -> Path:
        return self.root / ".git" / "hooks"


class TestInstalling(GitRepoTestCase):
    def test_every_hook_is_written_and_executable(self):
        installed, skipped = install(self.root)

        self.assertEqual(sorted(installed), sorted(HOOKS))
        self.assertEqual(skipped, [])
        for hook in HOOKS:
            path = self.hooks / hook
            self.assertTrue(path.is_file())
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR, f"{hook} is not executable")

    def test_every_hook_exits_zero_whatever_the_verdict(self):
        """Sentinel reports; it does not block. A non-zero exit aborts the push."""
        install(self.root)
        for hook in HOOKS:
            body = (self.hooks / hook).read_text(encoding="utf-8")
            with self.subTest(hook=hook):
                self.assertIn("exit 0", body)
                self.assertNotIn("exit 1", body)

    def test_a_hook_someone_else_wrote_is_never_overwritten(self):
        """A project may already run its tests on commit. Do not eat that."""
        theirs = self.hooks / "pre-commit"
        theirs.write_text("#!/bin/sh\nnpm test\n", encoding="utf-8")

        installed, skipped = install(self.root)

        self.assertEqual(theirs.read_text(encoding="utf-8"), "#!/bin/sh\nnpm test\n")
        self.assertNotIn("pre-commit", installed)
        self.assertTrue(any("pre-commit" in item for item in skipped))

    def test_reinstalling_over_our_own_hook_is_fine(self):
        install(self.root)
        installed, skipped = install(self.root)
        self.assertEqual(sorted(installed), sorted(HOOKS))
        self.assertEqual(skipped, [])

    def test_a_directory_without_git_is_refused_with_a_useful_message(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(ConfigError) as caught:
                install(Path(plain))
        self.assertIn("git repository", str(caught.exception))

    def test_the_script_names_the_interpreter_and_the_repo(self):
        install(self.root, python="/usr/bin/python3", package_root=Path("/pkg"))
        body = (self.hooks / "pre-push").read_text(encoding="utf-8")
        self.assertIn("/usr/bin/python3", body)
        self.assertIn(str(self.root), body)
        self.assertIn("/pkg", body)

    def test_the_script_falls_back_when_the_command_cannot_be_read(self):
        """Better to record `git push` honestly than to invent flags."""
        script = hook_script("pre-push", self.root, "python", Path("/pkg"))
        self.assertIn('COMMAND="git push"', script)


class TestUninstalling(GitRepoTestCase):
    def test_it_removes_our_hooks(self):
        install(self.root)
        removed = uninstall(self.root)

        self.assertEqual(sorted(removed), sorted(HOOKS))
        for hook in HOOKS:
            self.assertFalse((self.hooks / hook).exists())

    def test_it_leaves_hooks_we_did_not_write(self):
        theirs = self.hooks / "pre-commit"
        theirs.write_text("#!/bin/sh\nnpm test\n", encoding="utf-8")
        install(self.root)

        uninstall(self.root)

        self.assertTrue(theirs.is_file())
        self.assertEqual(theirs.read_text(encoding="utf-8"), "#!/bin/sh\nnpm test\n")

    def test_uninstalling_twice_is_harmless(self):
        install(self.root)
        uninstall(self.root)
        self.assertEqual(uninstall(self.root), [])


class TestRecognisingOurOwnHooks(GitRepoTestCase):
    def test_ours_is_recognised(self):
        install(self.root)
        self.assertTrue(is_ours(self.hooks / "pre-push"))

    def test_someone_elses_is_not(self):
        theirs = self.hooks / "pre-push"
        theirs.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        self.assertFalse(is_ours(theirs))

    def test_a_binary_hook_does_not_crash_the_check(self):
        weird = self.hooks / "pre-push"
        weird.write_bytes(b"\x7fELF\x00\x00binary")
        self.assertFalse(is_ours(weird))


class TestJudgingRealCommands(GitRepoTestCase):
    def test_a_force_push_is_blocked_with_a_reason(self):
        decision = record_command(self.root, "/usr/bin/git push --force origin main")

        self.assertIs(decision.verdict, Verdict.BLOCKED)
        self.assertTrue(decision.reasons)
        self.assertIn("push --force", decision.reasons[0])

    def test_a_rebase_is_flagged(self):
        self.assertIs(record_command(self.root, "git rebase -i HEAD~3").verdict, Verdict.FLAGGED)

    def test_an_ordinary_commit_is_allowed(self):
        decision = record_command(self.root, "git commit -m 'add discount field'")
        self.assertIs(decision.verdict, Verdict.ALLOWED)

    def test_the_command_lands_in_the_session_for_the_orchestrator(self):
        SessionLog(self.root).start()
        record_command(self.root, "/usr/bin/git push --force origin main")

        decisions = SessionLog(self.root).decisions()
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0].action.type, ActionType.GIT)
        self.assertEqual(decisions[0].action.command, "git push --force origin main")
        self.assertIs(decisions[0].verdict, Verdict.BLOCKED)

    def test_a_git_command_is_not_reported_as_a_file_change(self):
        """It changed no file, so the explainer has nothing to say about it."""
        SessionLog(self.root).start()
        record_command(self.root, "git push --force origin main")
        self.assertEqual(SessionLog(self.root).recorded(), [])

    def test_recording_without_a_session_still_returns_a_verdict(self):
        """The hooks run whether or not anyone is watching."""
        self.assertIs(record_command(self.root, "git push --force").verdict, Verdict.BLOCKED)


if __name__ == "__main__":
    unittest.main()
