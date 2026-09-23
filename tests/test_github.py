"""Tests for the shared GitHub client, with no network calls.

`urlopen` is stubbed, so these run offline and deterministically. What matters
here is the error handling: Sentinel must degrade to "unable to verify X" with
a message the user can act on, never a raw traceback mid-demo
(instructions.md #7).

    python -m unittest tests.test_github -v
"""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.shared.github import GitHubClient, GitHubError, parse_repo


def response(payload) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.github.com/x", code=code, msg="err", hdrs=headers or {}, fp=None
    )


class TestRepoReferences(unittest.TestCase):
    def test_owner_repo(self):
        self.assertEqual(parse_repo("psf/requests"), ("psf", "requests"))

    def test_full_url(self):
        self.assertEqual(parse_repo("https://github.com/psf/requests"), ("psf", "requests"))

    def test_trailing_slash(self):
        self.assertEqual(parse_repo("https://github.com/psf/requests/"), ("psf", "requests"))

    def test_nonsense_is_rejected_with_an_example(self):
        with self.assertRaises(GitHubError) as caught:
            parse_repo("requests")
        self.assertIn("psf/requests", str(caught.exception))


class TestFetching(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient(token="test-token")

    def test_pull_request_files_are_parsed(self):
        payload = [
            {"filename": "src/main.py", "status": "modified", "additions": 10, "deletions": 3},
            {"filename": "tests/test_main.py", "status": "added", "additions": 40, "deletions": 0},
        ]
        with patch("urllib.request.urlopen", return_value=response(payload)):
            files = self.client.pull_request_files("owner", "repo", 1)

        self.assertEqual([f.path for f in files], ["src/main.py", "tests/test_main.py"])
        self.assertEqual(files[0].total_changes, 13)
        self.assertEqual(files[1].status, "added")

    def test_entries_without_a_filename_are_skipped(self):
        with patch("urllib.request.urlopen", return_value=response([{"additions": 1}])):
            self.assertEqual(self.client.pull_request_files("owner", "repo", 1), [])

    def test_commit_files_are_parsed(self):
        payload = {"files": [{"filename": "a.py", "status": "removed", "additions": 0, "deletions": 12}]}
        with patch("urllib.request.urlopen", return_value=response(payload)):
            files = self.client.commit_files("owner", "repo", "abc123")

        self.assertEqual(files[0].status, "removed")
        self.assertEqual(files[0].total_changes, 12)

    def test_churn_is_a_plain_commit_count(self):
        with patch("urllib.request.urlopen", return_value=response([{}, {}, {}])):
            self.assertEqual(self.client.commit_count_for_path("owner", "repo", "src/main.py"), 3)

    def test_authorization_header_is_sent_when_a_token_is_set(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["auth"] = request.get_header("Authorization")
            return response([])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.pull_request_files("owner", "repo", 1)

        self.assertEqual(captured["auth"], "Bearer test-token")

    def test_no_authorization_header_without_a_token(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["auth"] = request.get_header("Authorization")
            return response([])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            GitHubClient(token="").pull_request_files("owner", "repo", 1)

        self.assertIsNone(captured["auth"])


class TestErrorHandling(unittest.TestCase):
    def test_missing_repo_explains_what_to_check(self):
        with patch("urllib.request.urlopen", side_effect=http_error(404)):
            with self.assertRaises(GitHubError) as caught:
                GitHubClient(token="t").pull_request_files("owner", "nope", 1)
        self.assertIn("Not found", str(caught.exception))

    def test_rate_limit_without_a_token_suggests_setting_one(self):
        error = http_error(403, {"X-RateLimit-Remaining": "0"})
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitHubError) as caught:
                GitHubClient(token="").pull_request_files("owner", "repo", 1)

        message = str(caught.exception)
        self.assertIn("rate limit", message.lower())
        self.assertIn("GITHUB_TOKEN", message)

    def test_rate_limit_with_a_token_does_not_suggest_setting_one(self):
        error = http_error(403, {"X-RateLimit-Remaining": "0"})
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitHubError) as caught:
                GitHubClient(token="t").pull_request_files("owner", "repo", 1)
        self.assertIn("Wait for the limit to reset", str(caught.exception))

    def test_network_failure_is_not_a_traceback(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route to host")):
            with self.assertRaises(GitHubError) as caught:
                GitHubClient().pull_request_files("owner", "repo", 1)
        self.assertIn("Could not reach GitHub", str(caught.exception))

    def test_malformed_json_is_reported_clearly(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"not json")):
            with self.assertRaises(GitHubError) as caught:
                GitHubClient().pull_request_files("owner", "repo", 1)
        self.assertIn("wasn't JSON", str(caught.exception))

    def test_unexpected_shape_is_reported_clearly(self):
        with patch("urllib.request.urlopen", return_value=response({"unexpected": True})):
            with self.assertRaises(GitHubError) as caught:
                GitHubClient().pull_request_files("owner", "repo", 1)
        self.assertIn("Expected a list", str(caught.exception))


class TestPagination(unittest.TestCase):
    def test_a_full_page_triggers_another_fetch(self):
        pages = [
            response([{"filename": f"f{i}.py"} for i in range(100)]),
            response([{"filename": "last.py"}]),
        ]
        with patch("urllib.request.urlopen", side_effect=pages):
            files = GitHubClient(token="t").pull_request_files("owner", "repo", 1)
        self.assertEqual(len(files), 101)

    def test_a_short_page_stops_paging(self):
        with patch("urllib.request.urlopen", return_value=response([{"filename": "one.py"}])) as fake:
            GitHubClient(token="t").pull_request_files("owner", "repo", 1)
        self.assertEqual(fake.call_count, 1)


class TestReadOnly(unittest.TestCase):
    def test_the_client_exposes_no_write_methods(self):
        # Sentinel observes and reports; it does not act on the repository
        # (instructions.md #6). If this fails, someone added a write path.
        forbidden = ("post", "put", "patch", "delete", "merge", "comment", "create")
        methods = [name for name in dir(GitHubClient) if not name.startswith("_")]
        for name in methods:
            with self.subTest(method=name):
                self.assertFalse(
                    any(word in name.lower() for word in forbidden),
                    f"GitHubClient.{name} looks like it writes to GitHub",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
