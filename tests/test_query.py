"""Tests for the natural-language query interface.

Two things are being pinned here.

Routing: a developer asking "can we ship?" must land on the verdict every time,
for free, with no model involved. Routing is the one part of a chat interface
that has to behave identically on every run.

And the rule the whole project rests on: the model must never be in a position
to decide a verdict. `answer()` reads an `Assessment` that `synthesize()` has
already produced, and the one path that does reach a model is handed the
finished report rather than the raw signals.

All offline, against real temporary sessions.

    python -m unittest tests.test_query -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.offline import go_offline

from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.interfaces import query
from sentinel.interfaces.query import (
    ASK,
    FIX,
    HELP,
    RULES,
    STATUS,
    VERDICT,
    WHAT_CHANGED,
    WHY,
    Answer,
    answer,
    classify,
)
from sentinel.session import SessionLog

# The phrasings a person actually uses, and where each must land.
ROUTING = [
    ("can we ship?", VERDICT),
    ("can I ship this", VERDICT),
    ("is it safe to merge", VERDICT),
    ("should I merge it?", VERDICT),
    ("good to go?", VERDICT),
    ("/verdict", VERDICT),
    ("what did it just do?", WHAT_CHANGED),
    ("what changed", WHAT_CHANGED),
    ("what happened while I was out", WHAT_CHANGED),
    ("summarise the changes", WHAT_CHANGED),
    ("/changes", WHAT_CHANGED),
    ("why did you stop it?", WHY),
    ("what went wrong?", WHY),
    ("why", WHY),
    ("did it follow my rules?", RULES),
    ("any norms broken", RULES),
    ("/rules", RULES),
    ("how do I fix it", FIX),
    ("what do I tell the agent", FIX),
    ("/fix", FIX),
    ("are you watching?", STATUS),
    ("/status", STATUS),
    ("/help", HELP),
    ("what can you do", HELP),
    ("is the moon made of cheese", ASK),
]


class TestRouting(unittest.TestCase):
    def test_every_phrasing_lands_where_it_should(self):
        for question, expected in ROUTING:
            with self.subTest(question=question):
                self.assertEqual(classify(question), expected)

    def test_routing_is_free_and_identical_every_time(self):
        for question, _ in ROUTING:
            self.assertEqual(classify(question), classify(question.upper()))

    def test_punctuation_and_spacing_do_not_matter(self):
        self.assertEqual(classify("  CAN WE SHIP???  "), VERDICT)

    def test_specific_phrasings_beat_general_ones(self):
        """'what went wrong' is a why question, not a what-changed question."""
        self.assertEqual(classify("what went wrong?"), WHY)
        self.assertEqual(classify("what changed?"), WHAT_CHANGED)

    def test_an_unrecognised_question_is_not_silently_a_verdict(self):
        self.assertEqual(classify("hello there"), ASK)


class QueryTestCase(unittest.TestCase):
    def setUp(self):
        go_offline(self)
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def session_with_a_change(self) -> SessionLog:
        self.write("sentinel.config.json", CONFIG)
        self.write("src/app.py", "def run():\n    return 1\n")

        log = SessionLog(self.root)
        log.start()

        self.write("src/app.py", 'MODEL = "gpt-4o"\n\ndef run():\n    return 1\n')
        action = Action(type=ActionType.MODIFY, path="src/app.py")
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))
        return log


CONFIG = """{
  "norms": {
    "rules": [
      {"id": "no-hardcoded-model-ids",
       "statement": "Model ids belong in configuration.",
       "severity": "FLAGGED",
       "patterns": ["[\\"'](gpt-[0-9][\\\\w.\\\\-]*)[\\"']"]}
    ]
  }
}"""


class TestAnsweringWithNoSession(QueryTestCase):
    """Nothing recorded means Sentinel says so, never that all is well."""

    def test_it_says_there_is_no_session_and_how_to_start_one(self):
        for question in ("can we ship?", "what did it do?", "why?", "did it follow my rules?"):
            with self.subTest(question=question):
                text = answer(question, self.root).text
                self.assertIn("no recorded session", text)
                self.assertIn("action_monitor.watcher", text)

    def test_it_never_claims_the_change_is_safe(self):
        self.assertNotIn("SAFE", answer("can we ship?", self.root).text)


class TestAnsweringFromASession(QueryTestCase):
    def test_the_verdict_question_returns_a_decided_verdict(self):
        self.session_with_a_change()
        result = answer("can we ship?", self.root)

        self.assertEqual(result.intent, VERDICT)
        # REVIEW, because a FLAGGED norm was broken.
        self.assertIn("REVIEW", result.text)

    def test_the_rules_question_names_the_rule_the_file_and_the_line(self):
        self.session_with_a_change()
        text = answer("did it follow my rules?", self.root).text

        self.assertIn("no-hardcoded-model-ids", text)
        self.assertIn("src/app.py", text)
        self.assertIn("gpt-4o", text)

    def test_the_why_question_gives_reasons_and_what_was_unverified(self):
        self.session_with_a_change()
        text = answer("why did you stop it?", self.root).text

        self.assertIn("REVIEW", text)
        self.assertIn("could not check", text.lower())
        self.assertIn("unverified check is not a pass", text)

    def test_the_fix_question_returns_text_for_a_human_to_send(self):
        self.session_with_a_change()
        text = answer("how do I fix it", self.root).text

        self.assertIn("I will not send it for you", text)
        self.assertIn("gpt-4o", text)

    def test_status_reports_the_session_without_judging_it(self):
        self.session_with_a_change()
        text = answer("are you watching?", self.root).text

        self.assertIn("Session started", text)
        self.assertIn("file(s) touched", text)
        self.assertNotIn("REVIEW", text)

    def test_what_changed_describes_the_change_without_a_verdict(self):
        self.session_with_a_change()
        text = answer("what did it just do?", self.root).text

        self.assertIn("src/app.py", text)
        self.assertNotIn("REVIEW", text)

    def test_the_summary_is_not_printed_twice_when_no_model_is_available(self):
        """`explain_session` already falls back to exactly this summary."""
        self.session_with_a_change()
        text = answer("what changed?", self.root).text
        self.assertEqual(text.count("file(s) changed"), 1)

    def test_what_changed_costs_nothing_when_there_is_nothing_to_say(self):
        """No change means no diff to narrate, so no model call to make."""
        self.write("sentinel.config.json", CONFIG)
        SessionLog(self.root).start()
        self.assertIn("Nothing has changed", answer("what changed?", self.root).text)


class TestTheModelCannotDecideAnything(QueryTestCase):
    """These run with the model removed, so anything they still answer is computed."""

    def test_the_verdict_is_the_same_on_every_run(self):
        """If a model decided it, "can we ship?" could differ between runs."""
        self.session_with_a_change()
        first = answer("can we ship?", self.root)
        second = answer("can we ship?", self.root)

        self.assertEqual(first.text, second.text)
        self.assertIn("REVIEW", first.text)

    def test_the_deciding_intents_answer_fully_with_no_model_at_all(self):
        """Verdict, rules, why and status are computed, so they still work."""
        self.session_with_a_change()
        for question in ("can we ship?", "did it follow my rules?", "why?", "are you watching?"):
            with self.subTest(question=question):
                text = answer(question, self.root).text
                self.assertNotIn("No plain-English", text)
                self.assertGreater(len(text.strip()), 20)

    def test_an_unrecognised_question_falls_back_to_the_decided_report(self):
        self.session_with_a_change()
        result = answer("is the moon made of cheese?", self.root)

        self.assertEqual(result.intent, ASK)
        self.assertIn("REVIEW", result.text)


class TestItNeverRaises(QueryTestCase):
    """A chat bot that dies on one odd question is worse than one that shrugs."""

    def test_a_broken_config_is_reported_not_raised(self):
        self.write("sentinel.config.json", "{ not json")
        result = answer("can we ship?", self.root)
        self.assertIsInstance(result, Answer)
        self.assertIn("cannot", result.text.lower())

    def test_an_empty_question_still_returns_an_answer(self):
        self.assertIsInstance(answer("", self.root), Answer)

    def test_a_very_long_question_still_returns_an_answer(self):
        self.assertIsInstance(answer("why " * 5000, self.root), Answer)


class TestHelp(unittest.TestCase):
    def test_help_lists_what_can_be_asked(self):
        text = answer("/help", Path(".")).text
        for phrase in ("What did it just do", "Can we ship", "Why did you stop", "rules"):
            self.assertIn(phrase, text)

    def test_help_is_ascii_for_every_terminal(self):
        query.HELP_TEXT.encode("ascii")


if __name__ == "__main__":
    unittest.main()
