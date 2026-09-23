"""Tests for the dashboard's control plane: registry, guard, supervisor, routes.

Two things are being pinned here, and they are the two that would hurt.

**The guard.** The moment the dashboard could start processes and write config
files, `bound to 127.0.0.1` stopped being a boundary - any page in another
browser tab can reach loopback. Every test in `TestTheGuard` and
`TestMutatingRoutesAreGuarded` is there because without it, a visited web page
could drive somebody's machine.

**One config writer.** The GUI and the wizard must produce identical config for
identical choices, or a project set up through the page and one set up in the
terminal quietly diverge.

No watcher is ever really spawned here, and no test touches the developer's own
registry - `SENTINEL_HOME` is redirected for every one of them.

    python -m unittest tests.test_control -v
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.offline import go_offline

from sentinel import agents, supervisor
from sentinel.action_monitor.actions import Action, ActionType, Decision, Verdict
from sentinel.interfaces import dashboard, guard, telegram
from sentinel.norms import catalog
from sentinel.norms.config import load_norms
from sentinel.norms.wizard import build_norms, to_config, write_norms
from sentinel.session import SessionLog
from sentinel.shared.config_file import CONFIG_FILENAME


class ControlTestCase(unittest.TestCase):
    """A private registry and a temp repo, for every test."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._repo = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.addCleanup(self._repo.cleanup)

        self.root = Path(self._repo.name).resolve()
        patched = mock.patch.dict(os.environ, {"SENTINEL_HOME": self._home.name})
        patched.start()
        self.addCleanup(patched.stop)


class TestTheRegistry(ControlTestCase):
    def test_it_starts_empty_and_remembers_what_is_added(self):
        self.assertEqual(agents.load(), [])
        agent = agents.add(self.root)
        self.assertEqual([item.id for item in agents.load()], [agent.id])
        self.assertEqual(agent.root, self.root)
        self.assertEqual(agent.name, self.root.name)

    def test_adding_the_same_folder_twice_is_the_same_agent(self):
        first = agents.add(self.root)
        second = agents.add(str(self.root) + "/")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(agents.load()), 1)

    def test_removing_an_agent_deletes_nothing_inside_the_repo(self):
        """Taking a repo off a list is not a reason to destroy someone's work."""
        marker = self.root / "keep-me.py"
        marker.write_text("x = 1\n", encoding="utf-8")
        agent = agents.add(self.root)

        self.assertTrue(agents.remove(agent.id))
        self.assertEqual(agents.load(), [])
        self.assertTrue(marker.is_file())
        self.assertTrue(self.root.is_dir())

    def test_removing_something_that_is_not_there_says_so(self):
        self.assertFalse(agents.remove("nope"))

    def test_a_path_that_cannot_be_supervised_is_refused_with_a_reason(self):
        missing = self.root / "does-not-exist"
        with self.assertRaises(agents.RegistryError) as caught:
            agents.add(missing)
        self.assertIn(str(missing), str(caught.exception))

        a_file = self.root / "file.txt"
        a_file.write_text("x", encoding="utf-8")
        with self.assertRaises(agents.RegistryError) as caught:
            agents.add(a_file)
        self.assertIn("not a folder", str(caught.exception))

    def test_a_malformed_registry_says_how_to_fix_it(self):
        agents.registry_path().parent.mkdir(parents=True, exist_ok=True)
        agents.registry_path().write_text("{not json", encoding="utf-8")
        with self.assertRaises(agents.RegistryError) as caught:
            agents.load()
        self.assertIn("delete it", str(caught.exception))

    def test_one_unusable_entry_does_not_cost_every_other_agent(self):
        agents.add(self.root)
        raw = json.loads(agents.registry_path().read_text(encoding="utf-8"))
        raw["agents"].insert(0, {"broken": True})
        agents.registry_path().write_text(json.dumps(raw), encoding="utf-8")

        self.assertEqual(len(agents.load()), 1)


class TestTheGuard(unittest.TestCase):
    """The checks that stop another web page driving this machine."""

    def setUp(self):
        guard.reset_token()

    def test_a_mutating_request_needs_this_session_token(self):
        with self.assertRaises(guard.Refused) as caught:
            guard.check_token("")
        self.assertEqual(caught.exception.status, 401)

        with self.assertRaises(guard.Refused) as caught:
            guard.check_token("a-different-token")
        self.assertEqual(caught.exception.status, 403)

        guard.check_token(guard.token())  # the real one passes

    def test_a_foreign_origin_is_refused_and_our_own_is_not(self):
        for origin in ("https://evil.example", "http://192.168.1.10:8765", "null"):
            with self.subTest(origin=origin):
                with self.assertRaises(guard.Refused):
                    guard.check_origin(origin)

        for origin in ("http://localhost:8765", "http://127.0.0.1:8765", ""):
            with self.subTest(origin=origin):
                guard.check_origin(origin)

    def test_a_form_content_type_is_refused(self):
        """An HTML form is the one cross-origin POST that needs no JavaScript,
        and it cannot set a JSON content type."""
        for content_type in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data", ""):
            with self.subTest(content_type=content_type):
                with self.assertRaises(guard.Refused) as caught:
                    guard.check_content_type(content_type)
                self.assertEqual(caught.exception.status, 415)

        guard.check_content_type("application/json; charset=utf-8")

    def test_a_path_cannot_escape_by_traversal_or_a_null_byte(self):
        with self.assertRaises(guard.Refused):
            guard.safe_directory("/tmp/\x00/etc")

        with tempfile.TemporaryDirectory() as folder:
            inside = Path(folder).resolve()
            # Resolved before anything is decided, so `..` cannot mean one
            # thing when checked and another when used.
            self.assertEqual(guard.safe_directory(f"{inside}/sub/.."), inside)

        with self.assertRaises(guard.Refused) as caught:
            guard.safe_directory("/definitely/not/here")
        self.assertEqual(caught.exception.status, 404)

    def test_the_token_reaches_the_page_it_is_meant_for(self):
        page = guard.inject_token("<html><head><title>x</title></head><body></body></html>")
        self.assertIn(guard.token(), page)
        self.assertIn(guard.TOKEN_META, page)
        self.assertLess(page.index(guard.TOKEN_META), page.index("</head>"))

    def test_only_this_machine_may_connect(self):
        self.assertTrue(guard.is_loopback_client("127.0.0.1"))
        self.assertTrue(guard.is_loopback_client("::1"))
        self.assertFalse(guard.is_loopback_client("10.0.0.4"))
        self.assertFalse(guard.is_loopback_client("not-an-address"))


class TestOneConfigWriter(ControlTestCase):
    """The GUI and the wizard must not drift into writing different files."""

    def test_the_gui_path_writes_exactly_what_the_catalog_says(self):
        selected = ["no-hardcoded-credentials", "no-skipped-tests"]
        chosen = build_norms(selected, [])

        write_norms(self.root, chosen, Verdict.FLAGGED)
        written = json.loads((self.root / CONFIG_FILENAME).read_text(encoding="utf-8"))

        # Byte-identical to composing the same norms straight from the catalog.
        expected = to_config([catalog.by_id(norm_id) for norm_id in selected], Verdict.FLAGGED)
        self.assertEqual(written["norms"], expected)

    def test_custom_rules_become_real_norms_the_checker_can_load(self):
        write_norms(self.root, build_norms([], ["do not restyle the dashboard"]), Verdict.FLAGGED)

        loaded = load_norms(self.root / CONFIG_FILENAME)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, "do-not-restyle-the-dashboard")
        # No patterns, so it is a rule the model judges - which is the whole
        # point of the free-text box.
        self.assertFalse(loaded[0].is_deterministic)

    def test_an_unknown_catalog_id_costs_that_rule_and_nothing_else(self):
        chosen = build_norms(["no-skipped-tests", "a-rule-that-was-removed"], [])
        self.assertEqual([norm.id for norm in chosen], ["no-skipped-tests"])

    def test_every_other_section_of_the_config_survives_a_norms_change(self):
        (self.root / CONFIG_FILENAME).write_text(
            json.dumps({"paths": {"deny": ["secret/*"]}, "norms": {"rules": []}}, indent=2),
            encoding="utf-8",
        )
        write_norms(self.root, build_norms(["no-skipped-tests"], []), Verdict.BLOCKED)

        after = json.loads((self.root / CONFIG_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(after["paths"]["deny"], ["secret/*"])
        self.assertEqual(len(after["norms"]["rules"]), 1)

    def test_what_the_page_shows_round_trips_through_the_file(self):
        write_norms(self.root, build_norms(["no-skipped-tests"], ["stay on task"]), Verdict.BLOCKED)

        shown = dashboard.norms_document(self.root)
        self.assertTrue(shown["configured"])
        self.assertEqual(shown["selected"], ["no-skipped-tests"])
        self.assertEqual(shown["custom"], ["stay on task."])
        self.assertEqual(shown["severity"], Verdict.BLOCKED.value)

    def test_an_unconfigured_project_says_so_rather_than_looking_empty(self):
        shown = dashboard.norms_document(self.root)
        self.assertFalse(shown["configured"])
        self.assertEqual(shown["selected"], [])


class TestTheSupervisor(ControlTestCase):
    """Process lifecycle, without ever spawning a real watcher."""

    def setUp(self):
        super().setUp()
        supervisor.reset_jobs()
        self.agent = agents.add(self.root)

    def test_an_agent_nobody_started_is_stopped(self):
        self.assertEqual(supervisor.status(self.agent).state, supervisor.STOPPED)
        self.assertFalse(supervisor.status(self.agent).is_running)

    def test_a_dead_pid_is_not_reported_as_running(self):
        agents.set_pid(self.agent.id, 999_999)
        self.assertEqual(supervisor.status(agents.get(self.agent.id)).state, supervisor.STOPPED)

    def test_a_live_pid_that_is_not_ours_is_never_adopted(self):
        """Pids get reused. Reporting a text editor as a running agent would be
        worse than reporting nothing."""
        agents.set_pid(self.agent.id, os.getpid())  # alive, but not a watcher
        state = supervisor.status(agents.get(self.agent.id))
        self.assertEqual(state.state, supervisor.STOPPED)
        self.assertIn("no longer a Sentinel watcher", state.detail)

    def test_a_pid_we_cannot_check_is_unverified_rather_than_running(self):
        agents.set_pid(self.agent.id, os.getpid())
        with mock.patch.object(supervisor, "_looks_like_ours", return_value=None):
            state = supervisor.status(agents.get(self.agent.id))
        self.assertEqual(state.state, supervisor.UNVERIFIED)
        self.assertIn("could not be confirmed", state.detail)

    def _healthy_popen(self):
        """A process that starts and keeps running."""
        process = mock.Mock()
        process.pid = 4242
        process.poll.return_value = None
        def wait(timeout=None):
            # Still alive when start() checks it survived, and exits promptly
            # when stop() asks it to.
            if timeout == supervisor._STARTUP_CHECK_SECONDS:
                raise supervisor.subprocess.TimeoutExpired("watcher", timeout)
            return 0

        process.wait.side_effect = wait
        return process

    def test_starting_uses_the_command_line_entry_point_not_a_second_code_path(self):
        with mock.patch.object(supervisor.subprocess, "Popen", return_value=self._healthy_popen()) as popen:
            supervisor.start(self.agent)

        command = popen.call_args[0][0]
        self.assertIn("sentinel.action_monitor.watcher", command)
        self.assertIn(str(self.root), command)
        supervisor.stop(self.agent)

    def test_the_child_can_import_sentinel_wherever_the_dashboard_was_launched(self):
        """The watcher died on import once because its working directory was
        set to the watched repo. It is started with the package on PYTHONPATH
        instead, and never with a cwd of its own."""
        with mock.patch.object(supervisor.subprocess, "Popen", return_value=self._healthy_popen()) as popen:
            supervisor.start(self.agent)
        supervisor.stop(self.agent)

        self.assertIsNone(popen.call_args.kwargs.get("cwd"))
        path = popen.call_args.kwargs["env"]["PYTHONPATH"]
        self.assertIn(str(Path(supervisor.__file__).resolve().parent.parent), path.split(os.pathsep))

    def test_a_watcher_that_dies_at_once_is_reported_not_called_running(self):
        """Pressing Start and being told 'running' about a process that never
        ran is the worst outcome here: the repo is not being watched and the
        person believes it is."""
        dead = mock.Mock()
        dead.pid = 4243
        dead.poll.return_value = 1
        dead.returncode = 1
        dead.wait.return_value = 1

        supervisor.log_path(self.agent).parent.mkdir(parents=True, exist_ok=True)
        supervisor.log_path(self.agent).write_text("ModuleNotFoundError: No module named 'sentinel'\n", encoding="utf-8")

        with mock.patch.object(supervisor.subprocess, "Popen", return_value=dead):
            with self.assertRaises(supervisor.SupervisorError) as caught:
                supervisor.start(self.agent)

        self.assertIn("stopped immediately", str(caught.exception))
        self.assertIn("ModuleNotFoundError", str(caught.exception))

    def test_stopping_something_that_was_never_started_is_not_an_error(self):
        self.assertEqual(supervisor.stop(self.agent).state, supervisor.STOPPED)

    def test_a_watcher_that_exited_on_its_own_is_reported_with_its_code(self):
        fake = mock.Mock()
        fake.poll.return_value = 1
        fake.returncode = 1
        with mock.patch.dict(supervisor._running, {self.agent.id: fake}):
            state = supervisor.status(self.agent)
        self.assertEqual(state.state, supervisor.STOPPED)
        self.assertIn("exited with code 1", state.detail)


class TestJobs(ControlTestCase):
    """The slow features, which cannot be synchronous HTTP calls."""

    def setUp(self):
        super().setUp()
        go_offline(self)
        supervisor.reset_jobs()
        self.addCleanup(supervisor.reset_jobs)
        self.agent = agents.add(self.root)

    def _finished(self, job, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = supervisor.job(job.id)
            if current is not None and current.state != supervisor.RUNNING:
                return current
            time.sleep(0.01)
        self.fail("the job never finished")

    def test_a_job_returns_at_once_and_its_answer_arrives_later(self):
        """Narration takes ~40s and a full review ~1m46s, so `submit` has to
        return while the work is still running or the request would time out."""
        release = threading.Event()

        def slow(*_args, **_kwargs):
            release.wait(timeout=5)
            return mock.Mock(text="the answer")

        with mock.patch("sentinel.interfaces.query.answer", slow):
            job = supervisor.submit("explain", self.agent)
            self.assertEqual(supervisor.job(job.id).state, supervisor.RUNNING)
            release.set()
            self.assertEqual(self._finished(job).output, "the answer")

    def test_every_button_asks_through_the_query_seam(self):
        """The GUI must not reach past `query.answer()` into the orchestrator."""
        for kind in supervisor.JOB_QUESTIONS:
            with self.subTest(kind=kind):
                with mock.patch("sentinel.interfaces.query.answer", return_value=mock.Mock(text="ok")) as asked:
                    self._finished(supervisor.submit(kind, self.agent))
                self.assertEqual(asked.call_args[0][0], supervisor.JOB_QUESTIONS[kind])

    def test_a_failing_job_is_reported_not_raised(self):
        with mock.patch("sentinel.interfaces.query.answer", side_effect=RuntimeError("boom")):
            done = self._finished(supervisor.submit("review", self.agent))
        self.assertEqual(done.state, "failed")
        self.assertIn("boom", done.error)

    def test_an_unknown_job_names_the_real_ones(self):
        with self.assertRaises(supervisor.SupervisorError) as caught:
            supervisor.submit("delete-everything", self.agent)
        self.assertIn("explain", str(caught.exception))


class RouteTestCase(ControlTestCase):
    """A real server on a real port, driven over real HTTP."""

    def setUp(self):
        super().setUp()
        go_offline(self)
        guard.reset_token()
        supervisor.reset_jobs()

        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        self.port = self.server.server_address[1]
        # A short poll interval, because `shutdown()` waits for the serve loop
        # to notice. The default 0.5s turned fifteen fast tests into seven
        # seconds of waiting for nothing.
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, route, method="GET", body=None, token=True, content_type="application/json", origin=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{route}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
        )
        if token:
            request.add_header(guard.TOKEN_HEADER, guard.token())
        if content_type:
            request.add_header("Content-Type", content_type)
        if origin:
            request.add_header("Origin", origin)

        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class TestReadRoutes(RouteTestCase):
    def test_the_catalog_is_everything_the_checkbox_list_needs(self):
        status, body = self.call("/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["groups"]), 4)
        self.assertEqual(len(body["presets"]), 5)

        every = [norm for group in body["groups"] for norm in group["norms"]]
        self.assertEqual(len(every), 13)
        # A regex hit is a fact and a model's judgement is an opinion; the
        # person switching a rule on should be told which they are getting.
        self.assertEqual({norm["checked_by"] for norm in every}, {"pattern", "model"})

    def test_the_agent_list_carries_live_status(self):
        agents.add(self.root)
        status, body = self.call("/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(body["agents"][0]["status"]["state"], supervisor.STOPPED)
        self.assertTrue(body["agents"][0]["exists"])

    def test_browsing_needs_the_token_because_it_exposes_the_disk(self):
        status, _ = self.call(f"/api/browse?path={self.root}", token=False)
        self.assertEqual(status, 401)

        status, body = self.call(f"/api/browse?path={self.root}")
        self.assertEqual(status, 200)
        self.assertEqual(body["path"], str(self.root))

    def test_the_picker_offers_folders_and_says_which_are_already_set_up(self):
        (self.root / "project").mkdir()
        (self.root / "project" / CONFIG_FILENAME).write_text("{}", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "a-file.txt").write_text("x", encoding="utf-8")

        _status, body = self.call(f"/api/browse?path={self.root}")
        names = {entry["name"] for entry in body["entries"]}
        self.assertEqual(names, {"project"})  # no files, no noise folders
        self.assertTrue(body["entries"][0]["configured"])

    def test_an_unknown_route_is_a_404_not_a_crash(self):
        status, body = self.call("/api/nonsense")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_the_commands_panel_lists_only_real_entry_points(self):
        agent = agents.add(self.root)
        _status, body = self.call(f"/api/agents/{agent.id}/commands")

        self.assertTrue(body["commands"])
        runnable = [entry["command"] for entry in body["commands"] if entry["command"].startswith("python -m")]
        self.assertTrue(runnable)

        for command in runnable:
            self.assertIn(str(self.root), command)
            __import__(command.split()[2])  # raises if the panel names something gone

    def test_the_commands_panel_never_shows_the_real_bot_token(self):
        """It is a secret, and this panel is meant to be copied and pasted."""
        agent = agents.add(self.root)
        telegram.save_settings(telegram.TelegramSettings(token="123456:REAL-SECRET-TOKEN"))

        _status, body = self.call(f"/api/agents/{agent.id}/commands")
        panel = json.dumps(body)
        self.assertNotIn("REAL-SECRET-TOKEN", panel)
        self.assertIn("SENTINEL_TELEGRAM_TOKEN", panel)


class TestMutatingRoutesAreGuarded(RouteTestCase):
    """Without these, a page in another tab could drive this machine."""

    def test_adding_an_agent_needs_a_token(self):
        status, _ = self.call("/api/agents", "POST", {"folder": str(self.root)}, token=False)
        self.assertEqual(status, 401)
        self.assertEqual(agents.load(), [])

    def test_adding_an_agent_needs_a_json_content_type(self):
        status, _ = self.call(
            "/api/agents", "POST", {"folder": str(self.root)}, content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(status, 415)
        self.assertEqual(agents.load(), [])

    def test_a_request_from_another_site_is_refused(self):
        status, _ = self.call("/api/agents", "POST", {"folder": str(self.root)}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(agents.load(), [])

    def test_starting_an_agent_needs_a_token(self):
        agent = agents.add(self.root)
        status, _ = self.call(f"/api/agents/{agent.id}/start", "POST", {}, token=False)
        self.assertEqual(status, 401)

    def test_writing_norms_needs_a_token(self):
        agent = agents.add(self.root)
        status, _ = self.call(f"/api/agents/{agent.id}/norms", "PUT", {"selected": []}, token=False)
        self.assertEqual(status, 401)
        self.assertFalse((self.root / CONFIG_FILENAME).exists())


class TestTheFirstRunFlow(RouteTestCase):
    """Pick a folder, set the rules, start - with no terminal at all."""

    def test_a_folder_becomes_a_configured_agent(self):
        status, agent = self.call("/api/agents", "POST", {"folder": str(self.root)})
        self.assertEqual(status, 201)

        status, body = self.call(
            f"/api/agents/{agent['id']}/norms",
            "PUT",
            {"selected": ["no-skipped-tests"], "custom": ["do not touch the payment flow"], "severity": "FLAGGED"},
        )
        self.assertEqual(status, 200)
        self.assertTrue((self.root / CONFIG_FILENAME).is_file())

        # And the rules are on disk in the repo, so every later start reads
        # them and never asks again.
        self.assertEqual(body["selected"], ["no-skipped-tests"])
        self.assertEqual(len(load_norms(self.root / CONFIG_FILENAME)), 2)

    def test_a_bad_folder_is_refused_with_something_a_person_can_act_on(self):
        status, body = self.call("/api/agents", "POST", {"folder": str(self.root / "nope")})
        self.assertEqual(status, 400)
        self.assertIn("nothing at", body["error"])

    def test_removing_an_agent_stops_it_first(self):
        agent = agents.add(self.root)
        with mock.patch.object(supervisor, "stop") as stopped:
            status, _ = self.call(f"/api/agents/{agent.id}", "DELETE")
        self.assertEqual(status, 200)
        stopped.assert_called_once()
        self.assertEqual(agents.load(), [])

    def test_acting_on_an_agent_that_is_gone_says_so(self):
        status, body = self.call("/api/agents/nosuchid/start", "POST", {})
        self.assertEqual(status, 404)
        self.assertIn("another tab", body["error"])


class TestTheLiveViewNeverPaysForTheModel(ControlTestCase):
    """The dashboard rebuilds its document every few seconds.

    Judging the written norms takes about a minute of inference, so doing it on
    that timer would spend the whole budget answering a question nobody asked -
    and it is the exact mistake CLAUDE.md warns about: "signals are gathered
    per intent, not all at once".
    """

    def setUp(self):
        super().setUp()
        # A hard guard, not a hope. This suite has twice been fast only because
        # Ollama happened to be down.
        go_offline(self)
        write_norms(
            self.root,
            build_norms(["no-skipped-tests"], ["do not redesign the dashboard"]),
            Verdict.FLAGGED,
        )
        log = SessionLog(self.root)
        log.start()
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        action = Action(type=ActionType.MODIFY, path="app.py")
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))

    def test_the_exported_document_does_not_reach_the_model(self):
        with mock.patch("sentinel.orchestrator.review.check_statements", return_value=([], [])) as judged:
            dashboard.build_document(self.root, self.root / CONFIG_FILENAME)
        judged.assert_not_called()

    def test_the_norms_it_skipped_are_named_rather_than_dropped(self):
        """Quietly returning fewer findings would read as a pass."""
        document = dashboard.build_document(self.root, self.root / CONFIG_FILENAME)
        unchecked = " ".join(document["verdict"]["unchecked"])
        self.assertIn("do-not-redesign-the-dashboard", unchecked)
        # And an unverified check is never a pass.
        self.assertNotEqual(document["verdict"]["level"], "SAFE")

    def test_the_pattern_norms_still_run_because_they_are_free(self):
        (self.root / "test_thing.py").write_text('@pytest.mark.skip("flaky")\n', encoding="utf-8")
        log = SessionLog(self.root)
        action = Action(type=ActionType.MODIFY, path="test_thing.py")
        log.record(action, Decision(action=action, verdict=Verdict.ALLOWED))

        document = dashboard.build_document(self.root, self.root / CONFIG_FILENAME)
        self.assertIn("no-skipped-tests", [finding["norm_id"] for finding in document["findings"]])

    def test_asking_for_the_model_explicitly_still_runs_it(self):
        with mock.patch("sentinel.orchestrator.review.check_statements", return_value=([], [])) as judged:
            dashboard.build_document(self.root, self.root / CONFIG_FILENAME, use_model=True)
        judged.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestTheTelegramSetup(RouteTestCase):
    """Setting the bot up from the page, with nothing typed in a terminal.

    Two things are being protected. The token is a secret and this is the only
    place it is ever handled over HTTP; and the allow-list is a security
    boundary, so the page may make it easier to set but never weaker.
    """

    def test_settings_round_trip_and_are_readable_by_nobody_else(self):
        telegram.save_settings(telegram.TelegramSettings(token="123:abc", chats=[7, 7, 3], agent_id="x"))

        loaded = telegram.load_settings()
        self.assertEqual(loaded.token, "123:abc")
        self.assertEqual(loaded.chats, [3, 7])  # deduplicated and sorted

        mode = stat.S_IMODE(telegram.settings_path().stat().st_mode)
        self.assertEqual(mode, 0o600, "a bot token must not be readable by other users")

    def test_a_corrupt_settings_file_does_not_stop_setting_it_up_again(self):
        telegram.settings_path().parent.mkdir(parents=True, exist_ok=True)
        telegram.settings_path().write_text("{not json", encoding="utf-8")
        self.assertFalse(telegram.load_settings().configured)

    def test_the_token_is_never_in_what_the_page_is_given(self):
        telegram.save_settings(telegram.TelegramSettings(token="123456:SECRET-VALUE", chats=[5]))

        document = json.dumps(dashboard.telegram_document())
        self.assertNotIn("SECRET-VALUE", document)
        self.assertIn("...ALUE", document)

    def test_the_token_is_validated_before_it_is_stored(self):
        """A typo should be caught here, not by a bot that silently answers nobody."""
        with mock.patch.object(telegram.TelegramClient, "get_me", side_effect=telegram.TelegramError("rejected")):
            status, body = self.call("/api/telegram", "PUT", {"token": "nope"})

        self.assertEqual(status, 400)
        self.assertIn("rejected", body["error"])
        self.assertFalse(telegram.load_settings().configured)

    def test_a_good_token_is_stored_and_the_bot_is_named(self):
        with mock.patch.object(telegram.TelegramClient, "get_me", return_value={"username": "my_bot"}):
            status, body = self.call("/api/telegram", "PUT", {"token": "123:good"})

        self.assertEqual(status, 200)
        self.assertEqual(body["username"], "my_bot")
        self.assertEqual(telegram.load_settings().token, "123:good")

    def test_setting_up_the_bot_needs_a_token_like_every_other_write(self):
        for method, body in (("PUT", {"token": "x"}), ("POST", {}), ("DELETE", None)):
            with self.subTest(method=method):
                route = "/api/telegram/start" if method == "POST" else "/api/telegram"
                status, _ = self.call(route, method, body, token=False)
                self.assertEqual(status, 401)

    def test_a_bot_with_nobody_on_the_allow_list_refuses_to_start(self):
        telegram.save_settings(telegram.TelegramSettings(token="123:good"))
        status, body = self.call("/api/telegram/start", "POST", {})

        self.assertEqual(status, 400)
        self.assertIn("answer nobody", body["error"])

    def test_chats_are_only_ever_whole_numbers(self):
        telegram.save_settings(telegram.TelegramSettings(token="123:good"))
        for chats in (["12"], [True], "12", [1.5]):
            with self.subTest(chats=chats):
                status, _ = self.call("/api/telegram", "PUT", {"chats": chats})
                self.assertEqual(status, 400)
                self.assertEqual(telegram.load_settings().chats, [])

    def test_discovery_will_not_fight_the_running_bot_for_updates(self):
        """Telegram allows one poller per token; two would steal each other's
        messages and answer 409."""
        telegram.save_settings(telegram.TelegramSettings(token="123:good", chats=[5]))

        with mock.patch.object(supervisor, "bot_status", return_value=supervisor.Status(supervisor.RUNNING, 42)):
            status, body = self.call("/api/telegram/discover", "POST", {})

        self.assertEqual(status, 409)
        self.assertIn("Stop the bot first", body["error"])

    def test_discovery_reports_who_has_written_and_never_approves_them(self):
        telegram.save_settings(telegram.TelegramSettings(token="123:good"))
        updates = [
            {"message": {"chat": {"id": 99, "first_name": "Wasif"}, "text": "hello"}},
            {"message": {"chat": {"id": 99, "first_name": "Wasif"}, "text": "again"}},
            {"message": {"chat": {"id": -100, "title": "Team room"}, "text": "hi"}},
        ]
        with mock.patch.object(telegram.TelegramClient, "get_updates", return_value=updates):
            _status, body = self.call("/api/telegram/discover", "POST", {})

        self.assertEqual([chat["id"] for chat in body["chats"]], [99, -100])
        self.assertEqual(body["chats"][0]["name"], "Wasif")
        # Discovering somebody is not the same as letting them in.
        self.assertFalse(any(chat["approved"] for chat in body["chats"]))
        self.assertEqual(telegram.load_settings().chats, [])

    def test_forgetting_the_token_removes_it_from_disk(self):
        telegram.save_settings(telegram.TelegramSettings(token="123:good", chats=[5]))
        status, body = self.call("/api/telegram", "DELETE")

        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])
        self.assertFalse(telegram.settings_path().exists())


class TestDrivingSentinelFromTelegram(ControlTestCase):
    """`/watch` from your phone, so a session you start away from the desk is
    still supervised."""

    def setUp(self):
        super().setUp()
        self.agent = agents.add(self.root)
        self.settings = telegram.TelegramSettings(token="123:good", chats=[5], agent_id=self.agent.id)

    def test_a_question_is_never_mistaken_for_a_command(self):
        """`query.classify()` is keyword matching, which is right for questions
        and would be dangerous here. Only exact commands act."""
        for text in (
            "can we ship?",
            "please watch the payments folder",
            "what did it do?",
            "unwatch sounds like a plan",
            "/fix",
            "/status",
        ):
            with self.subTest(text=text):
                self.assertIsNone(telegram.control(text, self.settings))

    def test_start_is_deliberately_not_a_command(self):
        """Telegram puts a Start button in front of every new chat and people
        press it by reflex. That must not spawn a process."""
        self.assertIsNone(telegram.control("/start", self.settings))

    def test_agents_lists_the_repos_and_marks_the_current_one(self):
        answer = telegram.control("/agents", self.settings)
        self.assertIn(self.agent.name, answer)
        self.assertIn("*", answer)

    def test_use_switches_which_repo_is_answered_about(self):
        answer = telegram.control("/use 1", self.settings)
        self.assertIn(self.agent.name, answer)
        self.assertEqual(telegram.load_settings().agent_id, self.agent.id)

    def test_use_refuses_a_number_that_is_not_there(self):
        self.assertIn("no repo 7", telegram.control("/use 7", self.settings))
        self.assertIn("/agents", telegram.control("/use", self.settings))

    def test_watch_starts_the_real_watcher_and_remembers_its_pid(self):
        with mock.patch.object(supervisor, "start", return_value=supervisor.Status(supervisor.RUNNING, 321)) as started:
            answer = telegram.control("/watch", self.settings)

        started.assert_called_once()
        self.assertIn(self.agent.name, answer)
        self.assertEqual(agents.get(self.agent.id).pid, 321)

    def test_a_watcher_that_will_not_start_is_explained_not_swallowed(self):
        with mock.patch.object(supervisor, "start", side_effect=supervisor.SupervisorError("no such folder")):
            answer = telegram.control("/watch", self.settings)
        self.assertIn("no such folder", answer)

    def test_unwatch_stops_it(self):
        with mock.patch.object(supervisor, "stop", return_value=supervisor.Status(supervisor.STOPPED)) as stopped:
            answer = telegram.control("/unwatch", self.settings)

        stopped.assert_called_once()
        self.assertIn("Stopped watching", answer)
        self.assertEqual(agents.get(self.agent.id).pid, 0)

    def test_a_stranger_never_reaches_a_control_command(self):
        """The allow-list is checked before anything is acted on, so nobody
        outside it can start a process."""
        update = {"message": {"chat": {"id": 999}, "text": "/watch"}}
        with mock.patch.object(supervisor, "start") as started:
            self.assertIsNone(telegram.handle(update, self.root, permitted={5}))
        started.assert_not_called()
