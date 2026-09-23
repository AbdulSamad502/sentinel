"""Standalone tests for the Action Monitor (instructions.md #5).

Uses stdlib `unittest` rather than pytest — nothing here needs fixtures or
parametrisation badly enough to justify a new dependency (instructions.md #4).

    python -m unittest tests.test_action_monitor -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.action_monitor.actions import Action, ActionType, Verdict, worst
from sentinel.action_monitor.config import ConfigError, MonitorConfig, load_config
from sentinel.action_monitor.rules import check, check_all, matches, normalise_path

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


def modify(path: str) -> Action:
    return Action(type=ActionType.MODIFY, path=path)


def git(command: str) -> Action:
    return Action(type=ActionType.GIT, command=command)


class TestPathNormalisation(unittest.TestCase):
    def test_backslashes_become_forward_slashes(self):
        self.assertEqual(normalise_path(r"src\app\main.py"), "src/app/main.py")

    def test_leading_dot_slash_is_dropped(self):
        self.assertEqual(normalise_path("./src/main.py"), "src/main.py")

    def test_dotfiles_keep_their_leading_dot(self):
        # The bug this guards: lstrip('./') would turn '.env' into 'env' and
        # every dotfile rule would silently stop matching.
        self.assertEqual(normalise_path(".env"), ".env")
        self.assertEqual(normalise_path("./.env"), ".env")

    def test_empty_path_is_handled(self):
        self.assertEqual(normalise_path(""), "")
        self.assertEqual(normalise_path("."), "")


class TestMatching(unittest.TestCase):
    def test_star_crosses_directories(self):
        self.assertTrue(matches("src/deep/nested/file.py", "src/*"))

    def test_bare_directory_covers_its_contents(self):
        self.assertTrue(matches("node_modules/left-pad/index.js", "node_modules"))

    def test_matching_is_case_sensitive_on_every_platform(self):
        # fnmatch (not fnmatchcase) folds case on Windows only, which would give
        # teammates different verdicts from identical config.
        self.assertFalse(matches("SRC/main.py", "src/*"))

    def test_non_match(self):
        self.assertFalse(matches("docs/readme.md", "src/*"))


class TestChecksAgainstRepoConfig(unittest.TestCase):
    """Runs against the real sentinel.config.json shipped in the repo."""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config(REPO_CONFIG)

    def test_ordinary_edit_is_allowed(self):
        decision = check(modify("src/app/main.py"), self.config)
        self.assertIs(decision.verdict, Verdict.ALLOWED)
        self.assertEqual(decision.reasons, [])

    def test_denied_location_is_blocked(self):
        decision = check(modify(".git/config"), self.config)
        self.assertIs(decision.verdict, Verdict.BLOCKED)
        self.assertIn("denied location", decision.describe())

    def test_secrets_file_is_blocked(self):
        for path in (".env", ".env.production", "deploy/server.pem", "app/secrets.yaml"):
            with self.subTest(path=path):
                self.assertIs(check(modify(path), self.config).verdict, Verdict.BLOCKED)

    def test_agent_cannot_quietly_edit_its_own_permissions(self):
        # If the coding agent can widen sentinel.config.json, the whole
        # component is theatre.
        self.assertIs(check(modify("sentinel.config.json"), self.config).verdict, Verdict.BLOCKED)

    def test_dependency_change_is_flagged(self):
        for path in ("requirements.txt", "package-lock.json", "go.mod"):
            with self.subTest(path=path):
                decision = check(modify(path), self.config)
                self.assertIs(decision.verdict, Verdict.FLAGGED)
                self.assertIn("dependencies", decision.describe())

    def test_ci_workflow_change_is_flagged_not_blocked(self):
        self.assertIs(check(modify(".github/workflows/ci.yml"), self.config).verdict, Verdict.FLAGGED)

    def test_deletion_is_flagged_even_in_an_ordinary_file(self):
        decision = check(Action(type=ActionType.DELETE, path="src/app/main.py"), self.config)
        self.assertIs(decision.verdict, Verdict.FLAGGED)
        self.assertTrue(decision.reasons)

    def test_destructive_git_commands_are_blocked(self):
        for command in ("git push --force origin main", "git reset --hard HEAD~3", "git clean -fdx"):
            with self.subTest(command=command):
                self.assertIs(check(git(command), self.config).verdict, Verdict.BLOCKED)

    def test_rewriting_history_is_flagged(self):
        self.assertIs(check(git("git commit --amend --no-edit"), self.config).verdict, Verdict.FLAGGED)

    def test_ordinary_git_is_allowed(self):
        for command in ("git status", "git add -A", "git commit -m 'fix bug'", "git push origin feature"):
            with self.subTest(command=command):
                self.assertIs(check(git(command), self.config).verdict, Verdict.ALLOWED)

    def test_extra_whitespace_does_not_hide_a_dangerous_command(self):
        self.assertIs(check(git("git   push    --force"), self.config).verdict, Verdict.BLOCKED)

    def test_uppercase_does_not_hide_a_dangerous_command(self):
        self.assertIs(check(git("GIT PUSH --FORCE"), self.config).verdict, Verdict.BLOCKED)

    def test_worst_finding_wins_and_all_reasons_survive(self):
        # A delete (FLAGGED baseline) of a secrets file (BLOCKED) must land on
        # BLOCKED while still explaining both.
        decision = check(Action(type=ActionType.DELETE, path=".env"), self.config)
        self.assertIs(decision.verdict, Verdict.BLOCKED)
        self.assertGreaterEqual(len(decision.reasons), 2)


class TestAllowList(unittest.TestCase):
    def test_empty_allow_list_permits_anything_not_denied(self):
        config = MonitorConfig(allow=[], deny=["secret/*"])
        self.assertIs(check(modify("anywhere/at/all.py"), config).verdict, Verdict.ALLOWED)

    def test_work_outside_the_scoped_folders_is_flagged(self):
        config = MonitorConfig(allow=["src/*"], deny=[])
        self.assertIs(check(modify("src/main.py"), config).verdict, Verdict.ALLOWED)

        decision = check(modify("infra/terraform.tf"), config)
        self.assertIs(decision.verdict, Verdict.FLAGGED)
        self.assertIn("outside the folders", decision.describe())

    def test_deny_beats_allow(self):
        config = MonitorConfig(allow=["src/*"], deny=["src/generated/*"])
        self.assertIs(check(modify("src/generated/api.py"), config).verdict, Verdict.BLOCKED)


class TestVerdictOrdering(unittest.TestCase):
    def test_worst_of(self):
        self.assertIs(worst([Verdict.ALLOWED, Verdict.BLOCKED, Verdict.FLAGGED]), Verdict.BLOCKED)
        self.assertIs(worst([Verdict.ALLOWED, Verdict.FLAGGED]), Verdict.FLAGGED)
        self.assertIs(worst([]), Verdict.ALLOWED)

    def test_check_all_puts_the_worst_first(self):
        config = load_config(REPO_CONFIG)
        decisions = check_all(
            [modify("src/ok.py"), modify("requirements.txt"), modify(".env")],
            config,
        )
        self.assertEqual(
            [decision.verdict for decision in decisions],
            [Verdict.BLOCKED, Verdict.FLAGGED, Verdict.ALLOWED],
        )


class TestConfigLoading(unittest.TestCase):
    def _write(self, payload) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.write(payload if isinstance(payload, str) else json.dumps(payload))
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_repo_config_loads(self):
        config = load_config(REPO_CONFIG)
        self.assertIn(".env", config.protected_paths)
        self.assertIn("requirements.txt", config.dependency_patterns)

    def test_comment_keys_are_not_treated_as_rules(self):
        config = load_config(REPO_CONFIG)
        self.assertNotIn("_comment", config.protected_paths)
        self.assertFalse(any(key.startswith("_") for key in config.protected_paths))

    def test_missing_file_explains_itself(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(Path("does-not-exist.json"))
        self.assertIn("No permission config", str(caught.exception))

    def test_broken_json_explains_itself(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self._write("{not json"))
        self.assertIn("not valid JSON", str(caught.exception))

    def test_unknown_verdict_names_the_offending_key(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self._write({"protected_paths": {".env": "EXPLODE"}}))
        self.assertIn("EXPLODE", str(caught.exception))

    def test_unknown_action_type_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self._write({"action_verdicts": {"teleport": "FLAGGED"}}))
        self.assertIn("teleport", str(caught.exception))

    def test_empty_config_is_valid_and_permits_everything(self):
        config = load_config(self._write({}))
        self.assertIs(check(modify("anything.py"), config).verdict, Verdict.ALLOWED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
