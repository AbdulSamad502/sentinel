"""Asking Sentinel about a repo from your phone.

The point of this interface: you hand a coding agent a task, walk away, and can
still find out what it did and whether it is shippable - without a laptop.

Set it up in the dashboard ("Telegram bot"), which writes the token and the
allow-list to `~/.sentinel/telegram.json` and starts this for you. Or by hand:

    export SENTINEL_TELEGRAM_TOKEN=...             # from @BotFather
    export SENTINEL_TELEGRAM_ALLOWED_CHATS=12345   # your chat id
    python -m sentinel.interfaces.telegram /path/to/watched/repo

**No new dependency.** Telegram's Bot API is plain HTTPS JSON, and
`shared/github.py` already proves the stdlib-urllib pattern in this repo, so
there is nothing here `python-telegram-bot` would buy us (instructions.md #4).

Three rules this is built around:

  * The token lives in the environment, never in a config file. It is a secret,
    and this repo's own `no-hardcoded-credentials` norm would flag it.
  * The allow-list is mandatory. Anyone can find a Telegram bot by name, and the
    answers quote real source lines from your repo. With no allow-list set the
    bot answers nobody, and prints the chat id of whoever wrote to it so you can
    add yourself.
  * It replies; it never initiates. Answering the question an allow-listed human
    asked is the job. It does not push alerts, and it never messages the coding
    agent - `/fix` sends the correction text to *you*, to forward or not
    (instructions.md #6). The one exception is the pairing confirmation, sent
    once, at an explicit click during setup, to the chat you just approved.
  * It can start and stop **Sentinel's own watcher** (`/watch`, `/unwatch`), so
    a session you kick off from your phone is still supervised. That is Sentinel
    acting on itself, never on the code it is watching.
"""

import argparse
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path

from .query import HELP_TEXT, answer

API_ROOT = "https://api.telegram.org"

# Telegram rejects anything longer. Answers carry diffs and reports, so this is
# reached in normal use, not just in edge cases.
MAX_MESSAGE = 4096

# Long-poll: the request parks on Telegram's side until something arrives, so
# the loop is idle rather than spinning. The socket timeout has to outlive it.
POLL_SECONDS = 50
_SOCKET_MARGIN = 15

_BACKOFF_START = 2
_BACKOFF_MAX = 60


# Where the bot's credentials live. Deliberately NOT `sentinel.config.json`
# like every other setting in this project: that file is checked into git, and
# a bot token is a secret. Per-user, outside any repo, mode 0600.
SETTINGS_NAME = "telegram.json"


class TelegramError(RuntimeError):
    """Telegram refused or could not be reached. Message says what to do."""


@dataclass(frozen=True)
class TelegramSettings:
    """Everything the bot needs, and nothing a repo should carry.

    `chats` is the allow-list and it is a security boundary: empty means answer
    nobody. `agent_id` is which supervised repo the bot answers about - one
    token can only ever serve one repo, because Telegram refuses two pollers.
    """

    token: str = ""
    chats: list[int] = field(default_factory=list)
    agent_id: str = ""
    pid: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def masked(self) -> str:
        """The token as the page is allowed to see it - never in full."""
        return f"...{self.token[-4:]}" if len(self.token) > 4 else ""


def settings_path() -> Path:
    from ..agents import home

    return home() / SETTINGS_NAME


def load_settings() -> TelegramSettings:
    """The saved bot settings, falling back to the environment.

    The environment still wins nothing - it fills in what the file does not
    have - so the documented `export SENTINEL_TELEGRAM_TOKEN=...` path keeps
    working for anyone who prefers it.
    """
    raw: dict = {}
    path = settings_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            # A corrupt settings file must not stop the bot being set up again.
            raw = {}

    chats = raw.get("chats")
    if not isinstance(chats, list):
        chats = []

    return TelegramSettings(
        token=str(raw.get("token") or os.environ.get("SENTINEL_TELEGRAM_TOKEN", "")).strip(),
        chats=sorted({int(chat) for chat in chats if isinstance(chat, int) and not isinstance(chat, bool)})
        or sorted(allowed_chats()),
        agent_id=str(raw.get("agent_id", "")),
        pid=raw.get("pid", 0) if isinstance(raw.get("pid"), int) else 0,
    )


def save_settings(settings: TelegramSettings) -> None:
    """Write the settings, readable by this user only."""
    path = settings_path()
    document = {
        "_comment": "Sentinel's Telegram bot. Contains a secret - never commit this file.",
        "token": settings.token,
        "chats": settings.chats,
        "agent_id": settings.agent_id,
        "pid": settings.pid,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        # After writing, not before: the file exists for an instant either way,
        # and chmod on a path that does not exist yet would simply fail.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise TelegramError(f"Could not save the bot settings to {path}: {exc}") from exc


def clear_settings() -> None:
    """Forget the token entirely."""
    try:
        settings_path().unlink(missing_ok=True)
    except OSError as exc:
        raise TelegramError(f"Could not remove {settings_path()}: {exc}") from exc


class TelegramClient:
    """The smallest client that does the job. Read and reply, nothing else."""

    def __init__(self, token: str, api_root: str = API_ROOT, timeout: int = POLL_SECONDS + _SOCKET_MARGIN):
        if not token:
            raise TelegramError(
                "No bot token. Create a bot with @BotFather on Telegram, then:\n"
                "  export SENTINEL_TELEGRAM_TOKEN=<the token it gives you>"
            )
        self.token = token
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, params: dict) -> list | dict:
        url = f"{self.api_root}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(url, data=data)
        request.add_header("User-Agent", "sentinel-supervisor")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise TelegramError(self._explain(exc, method)) from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"Could not reach Telegram ({exc.reason}). Check the network connection.") from exc
        except json.JSONDecodeError as exc:
            raise TelegramError(f"Telegram returned something that wasn't JSON for {method}: {exc}") from exc

        if not payload.get("ok"):
            raise TelegramError(f"Telegram refused {method}: {payload.get('description', 'no reason given')}")
        return payload.get("result", [])

    def _explain(self, exc: urllib.error.HTTPError, method: str) -> str:
        """Turn an HTTP error into something the user can act on."""
        if exc.code == 401:
            return "Telegram rejected the bot token. Check SENTINEL_TELEGRAM_TOKEN against what @BotFather gave you."
        if exc.code == 409:
            return "Another copy of this bot is already polling. Stop it before starting a second one."
        if exc.code == 429:
            return "Telegram is rate limiting this bot. Wait a minute before trying again."
        return f"Telegram returned HTTP {exc.code} for {method}."

    def get_updates(self, offset: int, timeout: int = POLL_SECONDS) -> list[dict]:
        """Wait for incoming messages. Blocks until one arrives or `timeout`."""
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout})
        return result if isinstance(result, list) else []

    def get_me(self) -> dict:
        """Who this token belongs to. The cheapest way to validate one."""
        result = self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            self._call("sendMessage", {"chat_id": chat_id, "text": chunk})


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Cut a long answer into sendable pieces, on line breaks where possible.

    Verdicts and diffs are meant to be read, so breaking mid-line would make
    them harder to follow than the split itself costs.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def allowed_chats(raw: str | None = None) -> set[int]:
    """Chat ids permitted to ask. Empty means nobody, deliberately.

    An empty allow-list is treated as "answer nobody" rather than "answer
    everyone". Getting this backwards would publish a repo's contents to anyone
    who guessed the bot's name.
    """
    raw = raw if raw is not None else os.environ.get("SENTINEL_TELEGRAM_ALLOWED_CHATS", "")
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def extract(update: dict) -> tuple[int, str] | None:
    """The (chat id, text) of a message, or None if it isn't one.

    Telegram sends edits, joins, photos and much else down the same stream.
    Anything without text is not a question.
    """
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")
    if not isinstance(chat_id, int) or not isinstance(text, str) or not text.strip():
        return None
    return chat_id, text.strip()


# The commands that *do* something, as opposed to asking something.
#
# Deliberately exact slash commands and nothing else. `query.classify()` is
# keyword matching, which is right for questions and would be dangerous here -
# a question containing the word "watch" must never spawn a process. And
# deliberately not `/start`: Telegram puts a Start button in front of every new
# chat, and people press it by reflex.
CONTROL_HELP = """You can also drive Sentinel from here:

  /agents   - the repos I can watch
  /use 2    - answer about repo 2 from now on
  /watch    - start watching the current repo
  /unwatch  - stop watching it"""


def control(text: str, settings: TelegramSettings) -> str | None:
    """Act on a control command, or return None so it falls through to a question.

    Starting a watcher is Sentinel acting on **itself**, at the request of an
    allow-listed human - the same distinction the dashboard's Start button
    relies on. Nothing here touches the code being watched.
    """
    from .. import agents, supervisor

    command, _, argument = text.strip().partition(" ")
    command = command.lower()
    if command not in ("/agents", "/use", "/watch", "/unwatch"):
        return None

    listed = agents.load()
    if not listed:
        return "No repos are supervised yet. Add one in the Sentinel dashboard first."

    current = next((agent for agent in listed if agent.id == settings.agent_id), listed[0])

    if command == "/agents":
        lines = ["Repos I can watch:", ""]
        for index, agent in enumerate(listed, start=1):
            mark = "*" if agent.id == current.id else " "
            state = supervisor.status(agent).state
            lines.append(f" {mark} {index}. {agent.name} - {state}")
        lines += ["", "* is the one I answer about. Switch with /use <number>."]
        return "\n".join(lines)

    if command == "/use":
        if not argument.strip().isdigit():
            return "Which one? Try /agents to see the list, then /use 2."
        index = int(argument.strip())
        if not 1 <= index <= len(listed):
            return f"There is no repo {index}. I have {len(listed)}."

        chosen = listed[index - 1]
        save_settings(replace(settings, agent_id=chosen.id))
        return f"Now answering about {chosen.name}."

    if command == "/watch":
        try:
            state = supervisor.start(current)
        except supervisor.SupervisorError as exc:
            return f"Could not start watching {current.name}:\n{exc}"
        agents.set_pid(current.id, state.pid)
        return f"Watching {current.name}. Let your coding agent work, then ask me what it did."

    try:
        supervisor.stop(current)
    except supervisor.SupervisorError as exc:
        return f"Could not stop watching {current.name}:\n{exc}"
    agents.set_pid(current.id, 0)
    return f"Stopped watching {current.name}."


def handle(update: dict, root: Path, permitted: set[int]) -> tuple[int, str] | None:
    """What to reply to one update, or None to stay silent.

    Returning None rather than an error message for a stranger is deliberate:
    a bot that says "you are not authorised" confirms it exists and is worth
    probing. Silence gives nothing away.
    """
    message = extract(update)
    if message is None:
        return None

    chat_id, text = message
    if chat_id not in permitted:
        print(f"Ignored a message from chat id {chat_id} (not in the allow-list).", flush=True)
        return None

    # Control first, and only on an exact command. A stranger never reaches
    # this line, so nothing can be started by anyone not on the allow-list.
    settings = load_settings()
    acted = control(text, settings)
    if acted is not None:
        return chat_id, acted

    # Re-read each time, so the dashboard can point the bot at another repo
    # while it is running.
    target = _current_root(settings, root)
    return chat_id, answer(text, target).text


def _current_root(settings: TelegramSettings, fallback: Path) -> Path:
    """The repo the bot is answering about right now."""
    from .. import agents

    if settings.agent_id:
        agent = agents.get(settings.agent_id)
        if agent is not None and agent.root.is_dir():
            return agent.root
    return fallback


def run(client: TelegramClient, root: Path, permitted: set[int]) -> None:
    """Poll and reply until interrupted. Blocking.

    Network failures back off and retry rather than ending the loop: the bot is
    on stage during the demo, and a dropped connection must not be the end of it
    (instructions.md #7).
    """
    offset = 0
    backoff = _BACKOFF_START

    print(f"Sentinel is answering questions about {root}")
    print(f"Answering {len(permitted)} allow-listed chat(s). Ctrl-C to stop.\n")

    while True:
        try:
            updates = client.get_updates(offset)
            backoff = _BACKOFF_START
        except TelegramError as exc:
            print(f"  {exc}\n  Retrying in {backoff}s.", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
            continue

        for update in updates:
            offset = max(offset, update.get("update_id", 0) + 1)
            reply = handle(update, root, permitted)
            if reply is None:
                continue

            chat_id, text = reply
            try:
                client.send_message(chat_id, text)
                print(f"  answered chat {chat_id}", flush=True)
            except TelegramError as exc:
                # One failed reply must not lose the others in this batch.
                print(f"  could not reply to {chat_id}: {exc}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Answer questions about a watched repo over Telegram.")
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="the watched repo (default: whichever one the dashboard pointed the bot at)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    root = Path(args.path).resolve() if args.path else _current_root(settings, Path.cwd())
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    permitted = set(settings.chats)
    if not permitted:
        print(
            "No allow-list set, so this bot will answer nobody.\n"
            "\n"
            "Its answers quote real source lines from your repo, and anyone can find\n"
            "a Telegram bot by name - so it only replies to chat ids you name.\n"
            "\n"
            "Set it up in the dashboard (Telegram bot), or message your bot once,\n"
            "note the chat id it prints below, then:\n"
            "  export SENTINEL_TELEGRAM_ALLOWED_CHATS=<that id>\n"
            "and start it again.\n",
            file=sys.stderr,
        )

    try:
        client = TelegramClient(settings.token)
    except TelegramError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    print(HELP_TEXT + "\n")
    print(CONTROL_HELP + "\n")
    try:
        run(client, root, permitted)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
