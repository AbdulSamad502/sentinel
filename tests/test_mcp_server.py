"""Tests for the MCP server the coding agent consults.

Two properties are being pinned.

**Read-only.** This is the one interface a coding agent talks to directly, so
"Sentinel never acts" has to be true of its whole surface, not just its
intentions. There is a test that fails if a tool is ever added whose name
suggests it changes something.

**Honest about what it did not check.** `check_my_work` skips the rules that
need a model, because a minute-long call is useless inside an agent's edit
loop. It must say so - an agent told "no violations" will report itself done.

    python -m unittest tests.test_mcp_server -v
"""

import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The MCP SDK's own models trip a pydantic-settings warning on construction.
# Nothing here can fix it, and letting it print mid-run makes a green suite look
# broken. Narrow on purpose: only this message, not warnings in general.
warnings.filterwarnings("ignore", message=r".*incomplete definition.*")

from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.action_monitor.config import load_config
from sentinel.interfaces.mcp_server import _NO_SESSION, _changed_text, _check_text, _rules_text, build_server
from sentinel.norms.config import load_norms
from sentinel.orchestrator.session_review import consultation_summary
from sentinel.session import SessionLog

CONFIG = """{
  "paths": {"deny": ["vendor/*"]},
  "protected_paths": {".env": "BLOCKED"},
  "dependency_files": {"verdict": "FLAGGED", "patterns": ["requirements.txt"]},
  "norms": {
    "rules": [
      {"id": "no-hardcoded-model-ids",
       "statement": "Model ids belong in configuration.",
       "severity": "FLAGGED",
       "patterns": ["[\\"'](gpt-[0-9][\\\\w.\\\\-]*)[\\"']"]},
      {"id": "ask-before-design-changes",
       "statement": "Do not make design decisions alone.",
       "severity": "FLAGGED"}
    ]
  }
}"""


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)
        self.config = self.root / "sentinel.config.json"
        self.config.write_text(CONFIG, encoding="utf-8")

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def session_with_a_violation(self) -> SessionLog:
        self.write("src/app.py", "def run():\n    return 1\n")
        log = SessionLog(self.root)
        log.start()

        self.write("src/app.py", 'MODEL = "gpt-4o"\n\ndef run():\n    return 1\n')
        action = Action(type=ActionType.MODIFY, path="src/app.py")
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))
        return log


class TestTheRulesTheAgentIsGiven(ServerTestCase):
    def test_it_states_every_rule_in_plain_english(self):
        text = _rules_text(load_norms(self.config), load_config(self.config))
        self.assertIn("Model ids belong in configuration.", text)
        self.assertIn("Do not make design decisions alone.", text)

    def test_it_distinguishes_rules_that_are_enforced_from_rules_that_are_judged(self):
        """An agent should know which ones will actually catch it."""
        text = _rules_text(load_norms(self.config), load_config(self.config))
        self.assertIn("checked automatically", text)
        self.assertIn("judged by a reviewer", text)

    def test_singular_and_plural_both_read_correctly(self):
        text = _rules_text(load_norms(self.config), load_config(self.config))
        self.assertIn("1 needs a reviewer's judgement", text)
        self.assertNotIn("1 need a", text)

    def test_it_names_the_files_the_agent_must_not_touch(self):
        text = _rules_text(load_norms(self.config), load_config(self.config))
        self.assertIn(".env", text)
        self.assertIn("vendor/*", text)
        self.assertIn("requirements.txt", text)

    def test_a_project_with_no_rules_says_so_rather_than_looking_empty(self):
        bare = self.root / "bare.json"
        bare.write_text("{}", encoding="utf-8")
        self.assertIn("no explicit rules", _rules_text(load_norms(bare), load_config(bare)))


class TestCheckingTheAgentsWork(ServerTestCase):
    def test_it_names_the_rule_the_file_and_the_line(self):
        text = _check_text(self.session_with_a_violation(), self.config)
        self.assertIn("no-hardcoded-model-ids", text)
        self.assertIn("src/app.py:1", text)
        self.assertIn("gpt-4o", text)

    def test_it_says_what_it_did_not_check(self):
        """Silence on the judged rules would read as approval."""
        text = _check_text(self.session_with_a_violation(), self.config)
        self.assertIn("ask-before-design-changes", text)
        self.assertIn("not a clean bill of health", text)

    def test_a_clean_change_is_still_not_called_clean_overall(self):
        self.write("src/app.py", "def run():\n    return 1\n")
        log = SessionLog(self.root)
        log.start()
        self.write("src/app.py", "def run():\n    return 2\n")
        action = Action(type=ActionType.MODIFY, path="src/app.py")
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))

        text = _check_text(log, self.config)
        self.assertIn("No rule violations found", text)
        self.assertIn("not a clean bill of health", text)

    def test_it_reminds_the_agent_that_sentinel_changes_nothing(self):
        text = _check_text(self.session_with_a_violation(), self.config)
        self.assertIn("Sentinel reports; it changes nothing", text)

    def test_it_tells_the_agent_the_developer_sees_this_too(self):
        """The agent should not think it can quietly self-correct."""
        self.assertIn("developer sees this", _check_text(self.session_with_a_violation(), self.config))

    def test_no_changes_yet_is_said_plainly(self):
        log = SessionLog(self.root)
        log.start()
        self.assertIn("not changed anything", _check_text(log, self.config))
        self.assertIn("not changed anything", _changed_text(log))


class TestWithNoWatcherRunning(ServerTestCase):
    def test_the_agent_is_told_how_to_get_one_started(self):
        self.assertIn("action_monitor.watcher", _NO_SESSION)

    def test_it_still_points_the_agent_at_the_rules(self):
        """Rules work without a session, and are the most useful call anyway."""
        self.assertIn("sentinel_project_rules", _NO_SESSION)


class TestTheConsultationLog(ServerTestCase):
    """The human keeps oversight of a loop they are no longer inside."""

    def test_consultations_are_recorded_and_counted(self):
        log = self.session_with_a_violation()
        log.record_consultation("project_rules")
        log.record_consultation("check_my_work")
        log.record_consultation("check_my_work")

        summary = consultation_summary(SessionLog(self.root))
        self.assertIn("3 time(s)", summary)
        self.assertIn("check_my_work x2", summary)
        self.assertIn("project_rules x1", summary)

    def test_no_consultations_adds_nothing_to_the_report(self):
        self.session_with_a_violation()
        self.assertEqual(consultation_summary(SessionLog(self.root)), "")

    def test_consulting_without_a_session_is_dropped_not_crashed(self):
        SessionLog(self.root).record_consultation("project_rules")
        self.assertEqual(SessionLog(self.root).consultations(), [])

    def test_a_corrupt_line_does_not_lose_the_rest(self):
        log = self.session_with_a_violation()
        log.record_consultation("project_rules")
        with log.consultations_file.open("a", encoding="utf-8") as handle:
            handle.write('{"tool": "half-writ')

        self.assertEqual(len(SessionLog(self.root).consultations()), 1)


class TestTheServerIsReadOnly(ServerTestCase):
    """Sentinel never acts, and this is the surface an agent talks to."""

    def test_it_exposes_exactly_the_three_read_only_tools(self):
        server = build_server(self.root, self.config)
        names = {tool.name for tool in server._tool_manager.list_tools()}
        self.assertEqual(
            names,
            {"sentinel_project_rules", "sentinel_what_changed", "sentinel_check_my_work"},
        )

    def test_no_tool_name_suggests_it_changes_anything(self):
        """Fails loudly if someone later adds sentinel_fix_it or sentinel_revert."""
        server = build_server(self.root, self.config)
        forbidden = ("fix", "write", "edit", "revert", "delete", "apply", "block", "undo", "commit")
        for tool in server._tool_manager.list_tools():
            for word in forbidden:
                with self.subTest(tool=tool.name, word=word):
                    self.assertNotIn(word, tool.name.lower())

    def test_every_tool_has_a_description_the_agent_can_act_on(self):
        server = build_server(self.root, self.config)
        for tool in server._tool_manager.list_tools():
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.description and len(tool.description) > 40)


if __name__ == "__main__":
    unittest.main()
