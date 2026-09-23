"""Tests for the AWS providers: Bedrock, the hosted runtime, and the swap itself.

The test that matters most is at the bottom. Sentinel's verdict is
deterministic, so **changing the model provider must not change it**. If
swapping Ollama for Bedrock could move a verdict, something has leaked into the
model that was never supposed to be there, and the project's central promise is
broken. That is pinned here as a byte-for-byte comparison.

Everything else guards the two things a hosted endpoint gets wrong: spending
money it should not, and leaking something it should not. All offline - no AWS
call is made, and no network is touched.

    python -m unittest tests.test_aws -v
"""

import ast
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.offline import go_offline

from sentinel import llm, remote
from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.interfaces import agentcore_app
from sentinel.llm import AGENTCORE, BEDROCK, GROQ, OLLAMA, ModelSetupError
from sentinel.orchestrator.review import review_session
from sentinel.session import SessionLog

# Every setting llm.py reads, so a developer's own shell cannot decide what a
# test sees. This bit the suite before with SENTINEL_MODEL.
_SETTINGS = (
    "SENTINEL_PROVIDER",
    "SENTINEL_MODEL",
    "SENTINEL_BEDROCK_MODEL",
    "SENTINEL_AWS_REGION",
    "SENTINEL_AGENTCORE_ENDPOINT",
    "SENTINEL_AGENTCORE_ARN",
    "SENTINEL_AGENTCORE_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


@contextmanager
def settings(config: str | None = None, **env: str):
    """Run with a known environment and a known config file, and nothing else.

    `config` is the JSON text of a `sentinel.config.json`; None means there is
    no config file at all, which is the state Sentinel is in when installed as
    a package rather than run from the repo.
    """
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "sentinel.config.json"
        if config is not None:
            path.write_text(config, encoding="utf-8")

        saved = {name: os.environ.pop(name, None) for name in _SETTINGS}
        os.environ.update(env)
        try:
            with mock.patch.object(llm, "DEFAULT_CONFIG_PATH", path):
                yield
        finally:
            for name in _SETTINGS:
                os.environ.pop(name, None)
                if saved[name] is not None:
                    os.environ[name] = saved[name]


class TestProviderSelection(unittest.TestCase):
    """Which brain this installation talks to, and who gets to decide."""

    def test_the_default_is_the_local_model(self):
        with settings():
            self.assertEqual(llm.provider(), OLLAMA)

    def test_the_config_can_set_the_provider(self):
        with settings(config='{"model": {"provider": "bedrock"}}'):
            self.assertEqual(llm.provider(), BEDROCK)

    def test_the_environment_beats_the_config(self):
        with settings(config='{"model": {"provider": "bedrock"}}', SENTINEL_PROVIDER="ollama"):
            self.assertEqual(llm.provider(), OLLAMA)

    def test_an_unknown_provider_names_the_real_ones(self):
        with settings(SENTINEL_PROVIDER="openai"), self.assertRaises(ModelSetupError) as caught:
            llm.provider()
        for name in (OLLAMA, BEDROCK, AGENTCORE, GROQ):
            self.assertIn(name, str(caught.exception))

    def test_a_malformed_config_is_an_error_not_a_silent_fallback(self):
        """Quietly running on a different provider than the file asks for is
        exactly the class of quiet wrong answer this project exists to stop."""
        with settings(config="{not json"), self.assertRaises(ModelSetupError) as caught:
            llm.provider()
        self.assertIn("provider", str(caught.exception).lower())

    def test_a_missing_config_file_is_fine(self):
        with settings():
            self.assertEqual(llm.provider(), OLLAMA)
            self.assertEqual(llm.aws_region(), llm.DEFAULT_AWS_REGION)

    def test_the_region_falls_back_to_the_aws_cli_variables(self):
        with settings(AWS_REGION="eu-west-2"):
            self.assertEqual(llm.aws_region(), "eu-west-2")
        with settings(SENTINEL_AWS_REGION="ap-south-1", AWS_REGION="eu-west-2"):
            self.assertEqual(llm.aws_region(), "ap-south-1")

    def test_a_watched_repo_cannot_redirect_our_model_calls(self):
        """The settings come from Sentinel's own config, never the supervised
        project's - otherwise a repo we are watching could point us at a model
        it controls."""
        source = Path(llm.__file__).read_text(encoding="utf-8")
        self.assertIn("DEFAULT_CONFIG_PATH", source)
        self.assertNotIn("resolve_config_path", source)


class TestBedrock(unittest.TestCase):
    """The Bedrock provider, without calling AWS."""

    def test_it_is_built_from_the_configured_id_and_region(self):
        built = {}

        class FakeBedrockModel:
            def __init__(self, **kwargs):
                built.update(kwargs)

        with settings(SENTINEL_PROVIDER=BEDROCK, SENTINEL_BEDROCK_MODEL="some-model-id", SENTINEL_AWS_REGION="eu-west-2"):
            with mock.patch.dict(sys.modules, {"strands.models": mock.Mock(BedrockModel=FakeBedrockModel)}):
                llm.get_model(1234)

        self.assertEqual(built["model_id"], "some-model-id")
        self.assertEqual(built["region_name"], "eu-west-2")
        self.assertEqual(built["max_tokens"], 1234)

    def test_no_model_id_says_how_to_list_the_real_ones(self):
        with settings(SENTINEL_PROVIDER=BEDROCK, config='{"model": {"bedrock_model_id": ""}}'):
            with mock.patch.dict(sys.modules, {"strands.models": mock.Mock()}):
                with self.assertRaises(ModelSetupError) as caught:
                    llm.get_model()
        self.assertIn("list-foundation-models", str(caught.exception))

    def test_aws_failures_are_translated_into_something_actionable(self):
        class NoCredentialsError(Exception):
            pass

        translated = llm.actionable_error(NoCredentialsError("boom"))
        self.assertIsInstance(translated, ModelSetupError)
        self.assertIn("aws configure", str(translated))

    def test_a_client_error_is_read_out_of_its_payload(self):
        class ClientError(Exception):
            response = {"Error": {"Code": "AccessDeniedException"}}

        translated = llm.actionable_error(ClientError("denied"))
        self.assertIsInstance(translated, ModelSetupError)
        self.assertIn("Model access", str(translated))

    def test_an_unknown_failure_is_passed_through_unchanged(self):
        original = RuntimeError("something else entirely")
        self.assertIs(llm.actionable_error(original), original)


class TestTheHostedProvider(unittest.TestCase):
    """`agentcore`: the model lives somewhere else."""

    def test_there_is_no_local_model_and_it_says_so(self):
        with settings(SENTINEL_PROVIDER=AGENTCORE), self.assertRaises(ModelSetupError) as caught:
            llm.get_model()
        self.assertIn("run_agent", str(caught.exception))

    def test_run_agent_goes_to_the_runtime_and_never_loads_a_local_model(self):
        with settings(SENTINEL_PROVIDER=AGENTCORE):
            with mock.patch.object(remote, "ask", return_value="from the cloud") as asked:
                self.assertEqual(llm.run_agent("sys", "prompt", 99), "from the cloud")
        asked.assert_called_once_with("sys", "prompt", 99)

    def test_not_being_configured_names_both_ways_to_configure_it(self):
        with settings(SENTINEL_PROVIDER=AGENTCORE), self.assertRaises(ModelSetupError) as caught:
            remote.ask("sys", "prompt", 100)
        message = str(caught.exception)
        self.assertIn("SENTINEL_AGENTCORE_ENDPOINT", message)
        self.assertIn("SENTINEL_AGENTCORE_ARN", message)
        self.assertIn("SENTINEL_PROVIDER=ollama", message)

    def test_the_payload_carries_what_the_runtime_needs_and_nothing_else(self):
        with settings():
            payload = remote.payload_for("sys", "prompt", 512)
        self.assertEqual(set(payload), {"system", "prompt", "max_tokens", "client"})
        self.assertEqual(payload["max_tokens"], 512)

    def test_an_oversized_prompt_is_refused_before_it_costs_anything(self):
        with settings(SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations"):
            with self.assertRaises(ModelSetupError) as caught:
                remote.ask("sys", "x" * (remote.MAX_PROMPT_CHARS + 1), 100)
        self.assertIn("SENTINEL_PROVIDER=ollama", str(caught.exception))


class TestTheRuntimeTransport(unittest.TestCase):
    """What actually goes over the wire, and what must not."""

    def _reply(self, body: dict):
        return lambda request, timeout=None: BytesIO(json.dumps(body).encode("utf-8"))

    def test_it_posts_json_and_reads_the_text_back(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = dict(request.headers)
            return BytesIO(b'{"text": "the answer"}')

        with settings(SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations"):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                self.assertEqual(remote.ask("sys", "prompt", 256), "the answer")

        self.assertEqual(captured["url"], "https://runtime.example/invocations")
        self.assertEqual(captured["body"]["prompt"], "prompt")

    def test_the_token_is_sent_as_a_bearer_header_when_there_is_one(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured.update(request.headers)
            return BytesIO(b'{"text": "ok"}')

        with settings(
            SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations",
            SENTINEL_AGENTCORE_TOKEN="s3cret",
        ):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                remote.ask("sys", "prompt", 256)
        self.assertEqual(captured.get("Authorization"), "Bearer s3cret")

    def test_no_token_means_no_authorization_header(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured.update(request.headers)
            return BytesIO(b'{"text": "ok"}')

        with settings(SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations"):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                remote.ask("sys", "prompt", 256)
        self.assertNotIn("Authorization", captured)

    def test_a_token_is_never_sent_over_plaintext(self):
        """A private repo's diff and a bearer token are not going over http for
        anyone's convenience."""
        with settings(SENTINEL_AGENTCORE_ENDPOINT="http://runtime.example/invocations"):
            with mock.patch("urllib.request.urlopen") as opened:
                with self.assertRaises(ModelSetupError) as caught:
                    remote.ask("sys", "prompt", 256)
        opened.assert_not_called()
        self.assertIn("https://", str(caught.exception))

    def test_localhost_may_be_plaintext_so_a_deployment_can_be_tested(self):
        with settings(SENTINEL_AGENTCORE_ENDPOINT="http://localhost:8080/invocations"):
            with mock.patch("urllib.request.urlopen", self._reply({"text": "local"})):
                self.assertEqual(remote.ask("sys", "prompt", 256), "local")

    def test_a_rejected_token_never_appears_in_the_error(self):
        failure = urllib.error.HTTPError("https://runtime.example", 403, "Forbidden", {}, None)
        with settings(
            SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations",
            SENTINEL_AGENTCORE_TOKEN="s3cret-do-not-print",
        ):
            with mock.patch("urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(ModelSetupError) as caught:
                    remote.ask("sys", "prompt", 256)
        message = str(caught.exception)
        self.assertNotIn("s3cret-do-not-print", message)
        self.assertIn("SENTINEL_AGENTCORE_TOKEN", message)

    def test_being_rate_limited_says_so_and_offers_the_local_model(self):
        failure = urllib.error.HTTPError("https://runtime.example", 429, "Too Many", {}, None)
        with settings(SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations"):
            with mock.patch("urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(ModelSetupError) as caught:
                    remote.ask("sys", "prompt", 256)
        self.assertIn("SENTINEL_PROVIDER=ollama", str(caught.exception))

    def test_an_unreachable_runtime_is_not_a_traceback(self):
        with settings(SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations"):
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
                with self.assertRaises(ModelSetupError):
                    remote.ask("sys", "prompt", 256)

    def test_an_empty_or_errored_answer_is_never_passed_off_as_an_answer(self):
        for body in ({"text": "   "}, {"error": "refused"}, {}):
            with self.subTest(body=body):
                with settings(SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations"):
                    with mock.patch("urllib.request.urlopen", self._reply(body)):
                        with self.assertRaises(Exception):
                            remote.ask("sys", "prompt", 256)


class TestTheHostedService(unittest.TestCase):
    """The container AgentCore runs. It must never cost more than it should."""

    def setUp(self):
        agentcore_app.reset_rate_limit()
        self.addCleanup(agentcore_app.reset_rate_limit)

    def _ok(self, text="narrated"):
        return mock.patch.object(agentcore_app, "run_agent", return_value=text)

    def test_a_good_request_is_answered(self):
        with self._ok(), settings(SENTINEL_PROVIDER=BEDROCK):
            status, body = agentcore_app.handle({"system": "s", "prompt": "p"})
        self.assertEqual((status, body), (200, {"text": "narrated"}))

    def test_a_bad_body_is_refused_rather_than_crashing(self):
        for payload in (None, [], "text", {}, {"system": "s"}, {"prompt": "p"}, {"system": 1, "prompt": "p"}):
            with self.subTest(payload=payload):
                status, body = agentcore_app.handle(payload)
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    def test_an_oversized_prompt_is_refused(self):
        payload = {"system": "s", "prompt": "x" * (agentcore_app.MAX_PROMPT_CHARS + 1)}
        status, body = agentcore_app.handle(payload)
        self.assertEqual(status, 413)

    def test_the_token_budget_is_clamped_not_trusted(self):
        seen = {}

        def record(system, prompt, max_tokens):
            seen["max_tokens"] = max_tokens
            return "ok"

        for asked, expected in ((10, 10), (10_000_000, agentcore_app.MAX_TOKENS_CEILING), (-1, llm.DEFAULT_MAX_TOKENS), (True, llm.DEFAULT_MAX_TOKENS)):
            with self.subTest(asked=asked):
                with mock.patch.object(agentcore_app, "run_agent", record), settings(SENTINEL_PROVIDER=BEDROCK):
                    agentcore_app.handle({"system": "s", "prompt": "p", "max_tokens": asked})
                self.assertEqual(seen["max_tokens"], expected)

    def test_one_install_cannot_spend_the_whole_credit(self):
        with self._ok(), settings(SENTINEL_PROVIDER=BEDROCK):
            for _ in range(agentcore_app.RATE_LIMIT_CALLS):
                self.assertEqual(agentcore_app.handle({"system": "s", "prompt": "p", "client": "one"})[0], 200)
            status, body = agentcore_app.handle({"system": "s", "prompt": "p", "client": "one"})
        self.assertEqual(status, 429)
        self.assertIn("Rate limit", body["error"])

    def test_the_limit_is_per_install_not_global(self):
        with self._ok(), settings(SENTINEL_PROVIDER=BEDROCK):
            for _ in range(agentcore_app.RATE_LIMIT_CALLS):
                agentcore_app.handle({"system": "s", "prompt": "p", "client": "one"})
            self.assertEqual(agentcore_app.handle({"system": "s", "prompt": "p", "client": "two"})[0], 200)

    def test_the_window_eventually_lets_a_caller_back_in(self):
        window = agentcore_app.RATE_LIMIT_WINDOW_SECONDS
        for index in range(agentcore_app.RATE_LIMIT_CALLS):
            self.assertTrue(agentcore_app.within_rate_limit("one", now=index * 0.001))
        self.assertFalse(agentcore_app.within_rate_limit("one", now=1.0))
        self.assertTrue(agentcore_app.within_rate_limit("one", now=window + 2))

    def test_it_refuses_to_be_a_proxy_to_itself(self):
        """One misplaced environment variable would make the container call its
        own endpoint for every request, and the failure looks like a hang."""
        with settings(SENTINEL_PROVIDER=AGENTCORE):
            status, body = agentcore_app.handle({"system": "s", "prompt": "p"})
        self.assertEqual(status, 500)
        self.assertIn("call itself", body["error"])

    def test_a_model_failure_is_reported_not_raised(self):
        with mock.patch.object(agentcore_app, "run_agent", side_effect=RuntimeError("bedrock said no")):
            with settings(SENTINEL_PROVIDER=BEDROCK):
                status, body = agentcore_app.handle({"system": "s", "prompt": "p"})
        self.assertEqual(status, 502)
        self.assertIn("bedrock said no", body["error"])

    def test_the_service_has_no_way_to_reach_a_verdict(self):
        """It is a model, hosted - not the orchestrator. Nothing here may be
        able to produce a verdict, or the LLM would be deciding one from the
        far side of a network."""
        tree = ast.parse(Path(agentcore_app.__file__).read_text(encoding="utf-8"))

        imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        for module in imported:
            self.assertNotIn("orchestrator", module)
            self.assertNotIn("verdict", module)

        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for forbidden in ("synthesize", "review_session", "review_pull_request", "Assessment"):
            self.assertNotIn(forbidden, called)

    def test_request_bodies_are_never_written_to_the_log(self):
        """Bodies here are other people's source code."""
        source = Path(agentcore_app.__file__).read_text(encoding="utf-8")
        self.assertIn("def log_message", source)
        self.assertNotIn("self.rfile.read", source.split("def log_message")[1])


@contextmanager
def model_answering(text: str):
    """Every provider's transport stubbed to give the same answer.

    Deliberately stubbed at the *transport*, not at `run_agent` - the point of
    the test below is that provider dispatch really happens and still cannot
    change the outcome. Patching the seam itself would prove nothing.
    """

    class FakeAgent:
        def __init__(self, model=None, system_prompt=None, callback_handler=None):
            pass

        def __call__(self, prompt):
            return text

    modules = {
        "strands": mock.Mock(Agent=FakeAgent),
        "strands.models": mock.Mock(BedrockModel=lambda **kwargs: object()),
        "strands.models.ollama": mock.Mock(OllamaModel=lambda **kwargs: object()),
    }
    with mock.patch.dict(sys.modules, modules):
        with mock.patch.object(remote, "ask", return_value=text):
            yield


class TestSwappingTheProviderCannotMoveTheVerdict(unittest.TestCase):
    """The regression test for the whole of Phase 5.

    `synthesize()` is deterministic, so the same session must produce the same
    verdict no matter which provider answered. If this ever fails, a model has
    got into the decision.
    """

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)

        # A model-judged norm, so the one path where a model reports anything
        # at all is actually exercised on every provider.
        self.config = self.root / "sentinel.config.json"
        self.config.write_text(
            json.dumps(
                {
                    "norms": {
                        "rules": [
                            {
                                "id": "stay-on-task",
                                "statement": "Only change what was asked for.",
                                "severity": "FLAGGED",
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        log = SessionLog(self.root)
        log.start()
        (self.root / "src").mkdir()
        (self.root / "src" / "charge.py").write_text('KEY = "hardcoded"\n', encoding="utf-8")
        action = Action(type=ActionType.MODIFY, path="src/charge.py")
        log.record(action, Decision(action=action, verdict=Verdict.FLAGGED, reasons=["payments code"]))

    def _report(self, provider_name: str) -> str:
        with settings(
            SENTINEL_PROVIDER=provider_name,
            SENTINEL_MODEL="a-local-model",
            SENTINEL_BEDROCK_MODEL="a-bedrock-model",
            SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations",
            SENTINEL_GROQ_MODEL="a-groq-model",
            GROQ_API_KEY="gsk-not-a-real-key",
        ):
            with model_answering("[]"):
                assessment, _changes, _findings = review_session(self.root, self.config)
        return assessment.report()

    def test_the_report_is_byte_identical_across_every_provider(self):
        local = self._report(OLLAMA)
        self.assertEqual(local, self._report(BEDROCK))
        self.assertEqual(local, self._report(AGENTCORE))
        self.assertEqual(local, self._report(GROQ))

        # And it is a real report, not an empty string every provider agrees on.
        self.assertIn("REVIEW", local)
        self.assertIn("src/charge.py", local)
        # The model-judged norm was actually judged on all four, so the one
        # path where a model reports anything really did run.
        self.assertNotIn("need judgement", local)

    def test_every_provider_really_was_used(self):
        """Guards the test above: if dispatch silently fell back to one
        provider, the comparison would pass while proving nothing."""
        transports = (
            (OLLAMA, "strands"),
            (BEDROCK, "strands"),
            (AGENTCORE, "remote"),
            (GROQ, "strands"),
        )
        for name, transport in transports:
            with self.subTest(provider=name):
                with settings(
                    SENTINEL_PROVIDER=name,
                    SENTINEL_MODEL="a-local-model",
                    SENTINEL_BEDROCK_MODEL="a-bedrock-model",
                    SENTINEL_AGENTCORE_ENDPOINT="https://runtime.example/invocations",
                    SENTINEL_GROQ_MODEL="a-groq-model",
                    GROQ_API_KEY="gsk-not-a-real-key",
                ):
                    if transport == "remote":
                        with mock.patch.object(remote, "ask", return_value="answered") as asked:
                            self.assertEqual(llm.run_agent("s", "p"), "answered")
                        asked.assert_called_once()
                    else:
                        with model_answering("answered"):
                            self.assertEqual(llm.run_agent("s", "p"), "answered")

    def test_it_is_still_identical_when_no_provider_can_be_reached(self):
        """Only the stated *reason* a check could not run may differ - and it
        must, because the causes genuinely differ."""
        go_offline(self)
        assessments = {}
        for name in (OLLAMA, BEDROCK, AGENTCORE, GROQ):
            with settings(
                SENTINEL_PROVIDER=name,
                SENTINEL_GROQ_MODEL="a-groq-model",
                GROQ_API_KEY="gsk-not-a-real-key",
            ):
                assessments[name] = review_session(self.root, self.config)[0]

        for name in (BEDROCK, AGENTCORE, GROQ):
            self.assertEqual(assessments[name].verdict, assessments[OLLAMA].verdict)
            self.assertEqual(assessments[name].reasons, assessments[OLLAMA].reasons)
            self.assertEqual(assessments[name].conditions, assessments[OLLAMA].conditions)

        # The norm that needs a model went unchecked on every provider, and
        # that caps the verdict at REVIEW rather than passing quietly.
        for name in (OLLAMA, BEDROCK, AGENTCORE, GROQ):
            self.assertTrue(assessments[name].unchecked)


if __name__ == "__main__":
    unittest.main()
