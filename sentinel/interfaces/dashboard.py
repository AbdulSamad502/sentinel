"""The dashboard, and the small API behind it.

This used to be a viewer over one repo: you ran the wizard in a terminal,
started the watcher in another, and the page showed what they produced. That
is backwards for the surface people actually use, so the dashboard now drives
Sentinel instead of trailing it - point it at a folder, declare the rules,
press start.

**It still never decides anything.** Every verdict on this page came from
`synthesize()` by way of `export.build_document`, every answer from
`query.answer()`, and every rule written through `wizard.write_norms` - the
same functions the terminal uses. This module is routing and process
plumbing over things that already existed. The rule was never "the dashboard
may not do anything"; it was "the dashboard is not a second brain", and that
holds.

Two things it is worth being precise about, because both look like broken
promises and neither is:

  * *Sentinel never acts* means it never reverts, blocks, deletes or commits
    the code it is watching. Starting a watcher and writing
    `sentinel.config.json` are Sentinel acting on **itself**, at the explicit
    click of the person who owns the repo.
  * *Bound to 127.0.0.1* is still true and now insufficient on its own, because
    a page in another browser tab can reach loopback. Everything that mutates
    goes through `guard.py` first.

    python -m sentinel.interfaces.dashboard              # every agent
    python -m sentinel.interfaces.dashboard <repo>       # ... and add this one
"""

import argparse
import json
import signal
import sys
from dataclasses import replace
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import agents, supervisor
from ..action_monitor.actions import Verdict
from ..action_monitor.tree import IGNORED_DIRECTORIES
from ..norms import catalog
from ..norms.config import load_norms
from ..norms.wizard import build_norms, write_norms
from ..shared.config_file import CONFIG_FILENAME, ConfigError, resolve_config_path
from . import guard, telegram
from .export import build_document

DEFAULT_PORT = 8765

# Localhost only, deliberately. Never make this 0.0.0.0 for convenience: the
# document quotes real source lines out of a private repo, and the control
# routes start processes. `guard.py` is the second lock, not a replacement.
HOST = "127.0.0.1"

# A body big enough for a long list of custom rules, small enough that nothing
# can exhaust memory by describing a folder.
MAX_BODY_BYTES = 256 * 1024

_BUILT_DASHBOARD = Path(__file__).resolve().parent.parent.parent / "dashboard" / "dist"

# Folders never worth offering in a folder picker. Reuses the watcher's list so
# the two cannot disagree about what counts as noise.
_HIDDEN_IN_PICKER = set(IGNORED_DIRECTORIES)


def commands_for(root: Path) -> list[dict]:
    """The terminal equivalent of everything the interface offers.

    For the person who would rather type. Generated from one place so the panel
    cannot drift away from what the buttons actually run.
    """
    return [
        {"label": "Set the project's rules", "command": f"python -m sentinel.norms.wizard {root}"},
        {"label": "Start watching", "command": f"python -m sentinel.action_monitor.watcher {root}"},
        {"label": "What did it just do?", "command": f"python -m sentinel.explainer.explain {root}"},
        {"label": "... without spending a token", "command": f"python -m sentinel.explainer.explain {root} --plain"},
        {"label": "In the order it happened", "command": f"python -m sentinel.explainer.explain {root} --timeline"},
        {"label": "Can we ship it?", "command": f"python -m sentinel.orchestrator.session_review {root}"},
        {"label": "As a pull request comment", "command": f"python -m sentinel.orchestrator.session_review {root} --markdown"},
        {"label": "A message to send the coding agent", "command": f"python -m sentinel.orchestrator.session_review {root} --fix-prompt"},
        {"label": "Ask in your own words", "command": f'python -m sentinel.interfaces.query {root} "can we ship?"'},
        {"label": "Is this set up correctly?", "command": f"python -m sentinel.doctor {root}"},
        {"label": "Let the agent report its own git commands", "command": f"python -m sentinel.action_monitor.githooks --install {root}"},
        {"label": "Let the coding agent consult Sentinel", "command": f"python -m sentinel.interfaces.mcp_server {root}"},
        # The bot reads its token and allow-list from ~/.sentinel/telegram.json,
        # which the page writes. These are the equivalent by hand. The token is
        # a placeholder on purpose - the real one never appears in this panel.
        {"label": "Telegram: the bot token from @BotFather", "command": "export SENTINEL_TELEGRAM_TOKEN=<token>"},
        {"label": "Telegram: who may ask (empty means nobody)", "command": "export SENTINEL_TELEGRAM_ALLOWED_CHATS=<your chat id>"},
        {"label": "Telegram: answer questions about this repo", "command": f"python -m sentinel.interfaces.telegram {root}"},
    ]


def catalog_document() -> dict:
    """The built-in rules, shaped for a checkbox list."""
    return {
        "presets": [
            {"key": key, "label": label, "norm_ids": list(ids)}
            for key, (label, ids) in catalog.PRESETS.items()
        ],
        "groups": [
            {
                "title": title,
                "norms": [
                    {
                        "id": norm.id,
                        "statement": norm.statement,
                        "severity": norm.severity.value,
                        # A regex hit is a fact and a model's judgement is an
                        # opinion. The person choosing rules should know which
                        # kind they are switching on.
                        "checked_by": "pattern" if norm.is_deterministic else "model",
                    }
                    for norm in norms
                ],
            }
            for title, norms in catalog.GROUPS.items()
        ],
    }


def norms_document(root: Path) -> dict:
    """The rules this project currently holds its agent to."""
    config_path = root / CONFIG_FILENAME
    if not config_path.is_file():
        return {"configured": False, "severity": Verdict.FLAGGED.value, "selected": [], "custom": []}

    # Matched on statement as well as id. A free-text rule can generate an id
    # that collides with a catalog one ("stay on task" -> `stay-on-task`), and
    # matching on id alone would show the person's own wording as a ticked
    # catalog box and then quietly replace it on the next save.
    known = {(norm.id, norm.statement) for norm in catalog.all_norms()}
    current = load_norms(config_path)

    return {
        "configured": True,
        # The strictest rule in the file is what the severity control shows:
        # anything BLOCKED means this project chose to be stopped, not warned.
        "severity": (
            Verdict.BLOCKED.value
            if any(norm.severity is Verdict.BLOCKED for norm in current)
            else Verdict.FLAGGED.value
        ),
        "selected": [norm.id for norm in current if (norm.id, norm.statement) in known],
        "custom": [norm.statement for norm in current if (norm.id, norm.statement) not in known],
    }


def telegram_document() -> dict:
    """What the page is allowed to know about the bot.

    Never the token. The page gets the last four characters so a person can
    tell one bot from another, and nothing more.
    """
    settings = telegram.load_settings()
    target = agents.get(settings.agent_id) if settings.agent_id else None

    return {
        "configured": settings.configured,
        "token_hint": settings.masked(),
        "chats": settings.chats,
        "agent_id": target.id if target else "",
        "agent_name": target.name if target else "",
        "status": supervisor.bot_status(settings.pid).as_json(),
    }


def agent_document(agent: agents.Agent) -> dict:
    state = supervisor.status(agent)
    return {**agent.as_json(), "status": state.as_json(), "exists": agent.root.is_dir()}


def browse_document(path: Path) -> dict:
    """One folder's sub-folders, for the picker.

    Directories only. The picker chooses somewhere to supervise, and offering
    files would only invite picking one.
    """
    try:
        entries = sorted(
            (entry for entry in path.iterdir() if entry.is_dir() and entry.name not in _HIDDEN_IN_PICKER),
            key=lambda entry: entry.name.lower(),
        )
    except PermissionError:
        raise guard.Refused(403, f"No permission to read {path}.")
    except OSError as exc:
        raise guard.Refused(400, f"Could not read {path}: {exc}")

    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else "",
        "home": str(Path.home()),
        "entries": [
            {
                "name": entry.name,
                "path": str(entry),
                # A folder that already has one is a project someone set up
                # before; the picker says so rather than making them remember.
                "configured": (entry / CONFIG_FILENAME).is_file(),
                "is_git": (entry / ".git").exists(),
            }
            for entry in entries
        ],
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    """The built dashboard, plus the API it drives."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_BUILT_DASHBOARD), **kwargs)

    # --- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the base class names it this
        route = self.path.split("?")[0]
        if route == "/session.json" or route.startswith("/api/"):
            self._handle(lambda: self._get(route))
            return
        if not _BUILT_DASHBOARD.is_dir():
            self._send(404, {"error": "The dashboard has not been built. Run: cd dashboard && npm run build"})
            return
        if route in ("/", "/index.html"):
            self._send_index()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._handle(lambda: self._post(self.path.split("?")[0]))

    def do_PUT(self) -> None:  # noqa: N802
        self._handle(lambda: self._post(self.path.split("?")[0], method="PUT"))

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle(lambda: self._post(self.path.split("?")[0], method="DELETE"))

    def _handle(self, work) -> None:
        """Run one route, turning every failure into an answer.

        A dashboard that dies on one bad request takes the whole session with
        it (instructions.md #7).
        """
        if not guard.is_loopback_client(self.client_address[0]):
            self._send(403, {"error": "This dashboard serves this machine only."})
            return
        try:
            status, body = work()
        except guard.Refused as exc:
            self._send(exc.status, {"error": str(exc)})
        except (agents.RegistryError, supervisor.SupervisorError, ConfigError, telegram.TelegramError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the server
            self._send(500, {"error": f"Something went wrong: {exc}"})
        else:
            self._send(status, body)

    # --- reads ------------------------------------------------------------

    def _get(self, route: str) -> tuple[int, dict]:
        if route == "/api/catalog":
            return 200, catalog_document()

        if route == "/api/agents":
            return 200, {"agents": [agent_document(agent) for agent in agents.load()]}

        if route == "/api/telegram":
            guard.check_reading(self.headers)
            return 200, telegram_document()

        if route == "/api/browse":
            guard.check_reading(self.headers)
            return 200, browse_document(guard.safe_directory(self._query("path")))

        if route.startswith("/api/jobs/"):
            job = supervisor.job(route.rsplit("/", 1)[-1])
            if job is None:
                return 404, {"error": "No such job. It may have been forgotten after a restart."}
            return 200, job.as_json()

        if route == "/session.json":
            # The single-repo route the first dashboard served. Kept working so
            # an existing bookmark, or a teammate's muscle memory, still does.
            listed = agents.load()
            if not listed:
                return 404, {"error": "No repo is being supervised yet. Add one in the dashboard."}
            return 200, build_document(listed[0].root, resolve_config_path(listed[0].root))

        parts = route.strip("/").split("/")
        if len(parts) == 4 and parts[1] == "agents":
            agent = self._agent(parts[2])
            tail = parts[3]
            if tail == "session":
                return 200, build_document(agent.root, resolve_config_path(agent.root))
            if tail == "norms":
                return 200, norms_document(agent.root)
            if tail == "commands":
                return 200, {"commands": commands_for(agent.root)}
            if tail == "log":
                return 200, {"lines": supervisor.tail(agent)}

        return 404, {"error": f"No such route: {route}"}

    # --- writes -----------------------------------------------------------

    def _post(self, route: str, method: str = "POST") -> tuple[int, dict]:
        if method == "DELETE":
            # No body, so there is no content type to require.
            guard.check_origin(self.headers.get("Origin", ""))
            guard.check_token(self.headers.get(guard.TOKEN_HEADER, ""))
            body: dict = {}
        else:
            guard.check_mutating(self.headers)
            body = self._body()

        if route == "/api/agents" and method == "POST":
            agent = agents.add(body.get("folder", ""), str(body.get("name", "")))
            return 201, agent_document(agent)

        if route.startswith("/api/telegram"):
            return self._telegram(route, method, body)

        parts = route.strip("/").split("/")
        if len(parts) >= 3 and parts[1] == "agents":
            agent = self._agent(parts[2])

            if len(parts) == 3 and method == "DELETE":
                supervisor.stop(agent)
                agents.remove(agent.id)
                return 200, {"removed": agent.id}

            if len(parts) == 4:
                return self._agent_action(agent, parts[3], method, body)

        return 404, {"error": f"No such route: {route}"}

    def _telegram(self, route: str, method: str, body: dict) -> tuple[int, dict]:
        """Setting the bot up, entirely from the page.

        The token never comes back out of here, and no chat id is ever added
        without the person clicking a discovered one. The allow-list is a
        security boundary: the page makes it easier to set, never weaker.
        """
        settings = telegram.load_settings()
        tail = route[len("/api/telegram") :].strip("/")

        if method == "DELETE" and not tail:
            supervisor.stop_bot(settings.pid)
            telegram.clear_settings()
            return 200, telegram_document()

        if method == "PUT" and not tail:
            return 200, self._save_telegram(settings, body)

        if method != "POST":
            return 404, {"error": f"Cannot {method} {route}."}

        if tail == "discover":
            return 200, {"chats": self._discover(settings)}

        if tail == "test":
            # The one message Sentinel ever sends unasked-for, and it is not
            # unasked-for: a person pressed this, to their own chat, to confirm
            # pairing worked before they walk away from the machine.
            if not settings.chats:
                raise guard.Refused(400, "Approve a chat first - there is nobody to message.")
            client = self._client(settings)
            for chat in settings.chats:
                client.send_message(chat, "Sentinel is connected. Ask me 'what did it just do?' any time.")
            return 200, {"sent": len(settings.chats)}

        if tail == "start":
            if not settings.chats:
                raise guard.Refused(
                    400,
                    "This bot would answer nobody. Approve at least one chat before starting it.",
                )
            state = supervisor.start_bot(settings.pid)
            telegram.save_settings(replace(settings, pid=state.pid))
            return 200, telegram_document()

        if tail == "stop":
            supervisor.stop_bot(settings.pid)
            telegram.save_settings(replace(settings, pid=0))
            return 200, telegram_document()

        return 404, {"error": f"No such route: {route}"}

    def _save_telegram(self, settings: telegram.TelegramSettings, body: dict) -> dict:
        """A partial update: the token, the allow-list, or the target repo."""
        if "token" in body:
            token = str(body.get("token", "")).strip()
            if not token:
                raise guard.Refused(400, "That token is empty.")
            # Validated before it is stored, so a typo is caught here rather
            # than by a bot that silently answers nobody.
            who = telegram.TelegramClient(token, timeout=15).get_me()
            settings = replace(settings, token=token)
            telegram.save_settings(settings)
            return {**telegram_document(), "username": who.get("username", "")}

        if "chats" in body:
            chats = body.get("chats")
            if not isinstance(chats, list) or not all(
                isinstance(chat, int) and not isinstance(chat, bool) for chat in chats
            ):
                raise guard.Refused(400, "'chats' must be a list of chat ids.")
            settings = replace(settings, chats=sorted(set(chats)))

        if "agent_id" in body:
            agent_id = str(body.get("agent_id", ""))
            if agent_id and agents.get(agent_id) is None:
                raise guard.Refused(404, "No such agent.")
            settings = replace(settings, agent_id=agent_id)

        telegram.save_settings(settings)
        return telegram_document()

    def _client(self, settings: telegram.TelegramSettings) -> telegram.TelegramClient:
        if not settings.configured:
            raise guard.Refused(400, "No bot token yet. Paste the one @BotFather gave you first.")
        return telegram.TelegramClient(settings.token, timeout=15)

    def _discover(self, settings: telegram.TelegramSettings) -> list[dict]:
        """Who has written to this bot.

        Telegram allows exactly one poller per token, so this cannot run while
        the bot is answering - it would answer 409 and, worse, steal updates
        the bot needed.
        """
        if supervisor.bot_status(settings.pid).is_running:
            raise guard.Refused(
                409,
                "Stop the bot first. Telegram allows only one listener per token, "
                "so I cannot look for new chats while it is answering.",
            )

        seen: dict[int, dict] = {}
        for update in self._client(settings).get_updates(offset=0, timeout=0):
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if not isinstance(chat_id, int):
                continue
            name = chat.get("title") or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            )
            seen[chat_id] = {
                "id": chat_id,
                "name": name or chat.get("username") or "this chat",
                "approved": chat_id in settings.chats,
            }
        return list(seen.values())

    def _agent_action(self, agent: agents.Agent, action: str, method: str, body: dict) -> tuple[int, dict]:
        if action == "start" and method == "POST":
            state = supervisor.start(agent)
            agents.set_pid(agent.id, state.pid)
            return 200, {**agent_document(agent), "status": state.as_json()}

        if action == "stop" and method == "POST":
            state = supervisor.stop(agent)
            agents.set_pid(agent.id, 0)
            return 200, {**agent_document(agent), "status": state.as_json()}

        if action == "norms" and method == "PUT":
            selected = [str(item) for item in body.get("selected", []) if isinstance(item, str)]
            custom = [str(item) for item in body.get("custom", []) if isinstance(item, str)]
            severity = Verdict.BLOCKED if str(body.get("severity", "")) == Verdict.BLOCKED.value else Verdict.FLAGGED

            written = write_norms(agent.root, build_norms(selected, custom), severity)
            return 200, {"written": str(written), **norms_document(agent.root)}

        if action == "jobs" and method == "POST":
            return 202, supervisor.submit(str(body.get("kind", "")), agent).as_json()

        return 404, {"error": f"Cannot {method} {action} on an agent."}

    # --- plumbing ---------------------------------------------------------

    def _agent(self, agent_id: str) -> agents.Agent:
        agent = agents.get(agent_id)
        if agent is None:
            raise guard.Refused(404, "No such agent. It may have been removed in another tab.")
        return agent

    def _query(self, name: str) -> str:
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(self.path).query).get(name, [""])[0]

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise guard.Refused(413, "That request body is too large.")
        if length <= 0:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise guard.Refused(400, f"Body is not valid JSON: {exc}")
        if not isinstance(parsed, dict):
            raise guard.Refused(400, "Body must be a JSON object.")
        return parsed

    def _send_index(self) -> None:
        """The page, with this session's token in it."""
        try:
            html = (_BUILT_DASHBOARD / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._send(404, {"error": "The dashboard has not been built. Run: cd dashboard && npm run build"})
            return

        body = guard.inject_token(html).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # State changes while you watch it, so never let any of this be cached.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Quiet by default: a request log per poll would bury the useful output."""


def serve(port: int = DEFAULT_PORT, root: Path | None = None) -> None:
    if root is not None:
        agents.add(root)

    server = ThreadingHTTPServer((HOST, port), DashboardHandler)
    url = f"http://{HOST}:{port}/"

    print("Sentinel dashboard")
    guard.print_banner(url)
    if not _BUILT_DASHBOARD.is_dir():
        print("\n  (the dashboard is not built yet: cd dashboard && npm install && npm run build)")

    supervised = agents.load()
    print(f"\n  {len(supervised)} repo(s) supervised." if supervised else "\n  No repos yet - add one in the page.")
    print("  Localhost only. Ctrl-C to stop.\n")

    # `kill` should shut down the same way Ctrl-C does. That matters more than
    # usual now that watchers outlive the dashboard: being killed silently
    # would leave someone unaware that anything is still supervising.
    def _interrupt(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _interrupt)
    except ValueError:
        # Not the main thread. Ctrl-C still works.
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # Deliberately does NOT stop what it started. Closing the page must not
        # end the supervision you left running - that is the whole point of
        # being able to ask from your phone. Say what is still alive instead of
        # killing it quietly.
        still = supervisor.running_here()
        print("\nDashboard stopped.")
        if still:
            print(f"  {still} process(es) are still running so they keep supervising.")
            print("  Stop them from the page, or with: pkill -f sentinel.action_monitor.watcher")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Sentinel dashboard and the API behind it.")
    parser.add_argument("path", nargs="?", default=None, help="a repo to supervise (optional; adds it to the list)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    root = None
    if args.path is not None:
        root = Path(args.path).resolve()
        if not root.is_dir():
            print(f"Not a directory: {root}", file=sys.stderr)
            return 1

    serve(args.port, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
