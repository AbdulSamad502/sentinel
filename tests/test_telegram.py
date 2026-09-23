"""Tests for the Telegram interface, with no network calls.

`urlopen` is stubbed, the same way `test_github.py` does it, so these run
offline and deterministically.

The tests that matter most are the allow-list ones. This bot's answers quote
real source lines out of a private repo, and anyone can find a Telegram bot by
name - so "who is allowed to ask" is a security boundary, not a preference.

    python -m unittest tests.test_telegram -v
"""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.interfaces.telegram import (
    MAX_MESSAGE,
    TelegramClient,
    TelegramError,
    allowed_chats,
    extract,
    handle,
    split_message,
)


def response(payload) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def ok(result) -> io.BytesIO:
    return response({"ok": True, "result": result})


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="https://api.telegram.org/x", code=code, msg="err", hdrs={}, fp=None)


def message(chat_id: int, text: str, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


class RepoTestCase(unittest.TestCase):
    """An empty temp repo to ask about.

    Never `Path(".")`. That is Sentinel's own checkout, which usually has a live
    session in it, so a question would run a real norm check against a real
    model - turning a 0.2s offline suite into a 50s one that depends on whether
    Ollama happens to be up.
    """

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)


class TestTheAllowList(RepoTestCase):
    """Who may ask. Getting this backwards publishes a private repo."""

    def test_an_unset_allow_list_means_nobody_not_everybody(self):
        self.assertEqual(allowed_chats(""), set())
        self.assertEqual(allowed_chats("   "), set())

    def test_ids_are_parsed_from_a_comma_separated_list(self):
        self.assertEqual(allowed_chats("111, 222 ,333"), {111, 222, 333})

    def test_negative_ids_are_valid_because_group_chats_have_them(self):
        self.assertEqual(allowed_chats("-100123"), {-100123})

    def test_junk_entries_are_dropped_rather_than_crashing(self):
        self.assertEqual(allowed_chats("111, notanid, , 222"), {111, 222})

    def test_a_stranger_gets_no_reply_at_all(self):
        """Not even an error: a refusal confirms the bot is worth probing."""
        self.assertIsNone(handle(message(999, "can we ship?"), self.root, permitted={111}))

    def test_an_allow_listed_chat_gets_an_answer(self):
        reply = handle(message(111, "/help"), self.root, permitted={111})
        self.assertIsNotNone(reply)
        chat_id, text = reply
        self.assertEqual(chat_id, 111)
        self.assertIn("watching your repo", text)

    def test_with_an_empty_allow_list_nobody_is_answered(self):
        self.assertIsNone(handle(message(111, "/help"), self.root, permitted=set()))


class TestReadingUpdates(unittest.TestCase):
    def test_a_plain_message_is_extracted(self):
        self.assertEqual(extract(message(7, "hello")), (7, "hello"))

    def test_an_edited_message_still_counts_as_a_question(self):
        update = {"edited_message": {"chat": {"id": 7}, "text": "can we ship?"}}
        self.assertEqual(extract(update), (7, "can we ship?"))

    def test_updates_without_text_are_ignored(self):
        """Telegram sends joins, photos and stickers down the same stream."""
        self.assertIsNone(extract({"message": {"chat": {"id": 7}}}))
        self.assertIsNone(extract({"message": {"chat": {"id": 7}, "text": "   "}}))
        self.assertIsNone(extract({"my_chat_member": {"chat": {"id": 7}}}))
        self.assertIsNone(extract({}))

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(extract(message(7, "  what changed?  ")), (7, "what changed?"))


class TestSplittingLongAnswers(unittest.TestCase):
    def test_a_short_answer_is_one_message(self):
        self.assertEqual(split_message("short"), ["short"])

    def test_a_long_answer_is_cut_into_sendable_pieces(self):
        chunks = split_message("\n".join(f"line {n}" for n in range(2000)))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_MESSAGE)

    def test_it_breaks_on_line_endings_so_reports_stay_readable(self):
        text = "\n".join("x" * 50 for _ in range(10))
        chunks = split_message(text, limit=120)
        for chunk in chunks:
            self.assertFalse(chunk.startswith("x" * 50 + "x"), "a line was cut in half")

    def test_a_single_unbroken_line_is_still_split(self):
        chunks = split_message("y" * 300, limit=100)
        self.assertEqual(len(chunks), 3)

    def test_nothing_is_lost_in_the_split(self):
        text = "\n".join(f"line {n}" for n in range(500))
        self.assertEqual("".join(split_message(text, limit=200)).replace("\n", ""), text.replace("\n", ""))


class TestTheClient(unittest.TestCase):
    def test_a_missing_token_says_how_to_get_one(self):
        with self.assertRaises(TelegramError) as caught:
            TelegramClient("")
        self.assertIn("BotFather", str(caught.exception))

    def test_updates_are_returned(self):
        client = TelegramClient("token")
        with patch("urllib.request.urlopen", return_value=ok([message(1, "hi")])):
            self.assertEqual(len(client.get_updates(0)), 1)

    def test_a_rejected_token_is_explained_not_traced(self):
        client = TelegramClient("bad")
        with patch("urllib.request.urlopen", side_effect=http_error(401)):
            with self.assertRaises(TelegramError) as caught:
                client.get_updates(0)
        self.assertIn("SENTINEL_TELEGRAM_TOKEN", str(caught.exception))

    def test_a_second_running_copy_is_explained(self):
        client = TelegramClient("token")
        with patch("urllib.request.urlopen", side_effect=http_error(409)):
            with self.assertRaises(TelegramError) as caught:
                client.get_updates(0)
        self.assertIn("already polling", str(caught.exception))

    def test_an_unreachable_network_is_explained(self):
        client = TelegramClient("token")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(TelegramError) as caught:
                client.get_updates(0)
        self.assertIn("Could not reach Telegram", str(caught.exception))

    def test_an_ok_false_payload_is_an_error_not_a_silent_empty_result(self):
        client = TelegramClient("token")
        with patch("urllib.request.urlopen", return_value=response({"ok": False, "description": "chat not found"})):
            with self.assertRaises(TelegramError) as caught:
                client.get_updates(0)
        self.assertIn("chat not found", str(caught.exception))

    def test_a_long_reply_is_sent_as_several_messages(self):
        client = TelegramClient("token")
        # A fresh body per call: one BytesIO is consumed by the first send.
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: ok(True)) as opened:
            client.send_message(1, "\n".join(f"line {n}" for n in range(2000)))
        self.assertGreater(opened.call_count, 1)

    def test_the_token_is_never_put_in_a_query_string(self):
        """It goes in the path, and the body carries the parameters."""
        client = TelegramClient("secret-token")
        with patch("urllib.request.urlopen", return_value=ok([])) as opened:
            client.get_updates(0)
        request = opened.call_args[0][0]
        self.assertNotIn("?", request.full_url)
        self.assertIn("secret-token", request.full_url)


class TestItOnlyEverReplies(RepoTestCase):
    """Sentinel reports; it does not act (instructions.md #6)."""

    def test_the_client_has_no_method_that_acts_on_anything(self):
        """Read, identify yourself, reply. Nothing that changes anything.

        `get_me` is here because setup has to validate a token and name the
        bot; it is still a read. If this set ever grows a method that edits,
        deletes, pins, bans or joins, the bot has stopped being a reporter.
        """
        surface = {name for name in dir(TelegramClient) if not name.startswith("_")}
        self.assertEqual(surface, {"get_updates", "get_me", "send_message"})

    def test_asking_how_to_fix_returns_text_for_the_human_not_a_send(self):
        reply = handle(message(111, "how do i fix it"), self.root, permitted={111})
        self.assertIsNotNone(reply)
        _chat_id, text = reply
        self.assertNotIn("I have sent", text)


if __name__ == "__main__":
    unittest.main()
