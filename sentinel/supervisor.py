"""Starting, stopping and asking after the watcher processes.

The dashboard needed a way to run an agent without the developer opening a
terminal. It does that by spawning **the existing command line entry point** -
`python -m sentinel.action_monitor.watcher <root>` - as a subprocess, rather
than calling `watch()` in a thread.

That choice is deliberate and worth keeping. One code path means the GUI and
the terminal cannot drift into behaving differently, a crash in a watcher
cannot take the dashboard down with it, and anything a person learns from the
CLI panel is literally what the button does.

Also here: the **job runner** for the slow features. Narrating a change takes
about forty seconds and a full session review about a minute and a half, so
neither can be a synchronous HTTP call. A job is submitted, runs on a thread,
and is polled. Every job goes through `query.answer()` - the same seam the
Telegram bot and the CLI use - so a button in the UI can never reach past it
into the orchestrator (see CLAUDE.md).

Nothing here decides anything. It starts processes and collects text.
"""

import os
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .agents import Agent, home

# Where a watcher's output goes, next to the session it is recording. Inside
# `.sentinel/`, so Sentinel never observes its own writes.
LOG_NAME = "watcher.log"
BOT_LOG_NAME = "telegram.log"

RUNNING = "running"
STOPPED = "stopped"
UNVERIFIED = "unverified"

# How long a stopped process gets to exit politely before it is killed.
_TERMINATE_GRACE_SECONDS = 5

# How long to watch a freshly started watcher before believing it started.
# Long enough to catch an import or config failure, short enough that
# pressing Start still feels immediate.
_STARTUP_CHECK_SECONDS = 0.6

# What a process's command line must contain for us to believe a pid we did
# not start in this process is really ours.
_WATCHER_MODULE = "sentinel.action_monitor.watcher"
_BOT_MODULE = "sentinel.interfaces.telegram"

# The bot is a singleton - Telegram refuses two pollers on one token - so it
# gets one fixed key rather than one per agent.
_BOT_KEY = "__bot__"

# Live processes this dashboard started, by agent id. Authoritative while the
# server is up: `poll()` cannot be fooled by pid reuse.
_running: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


class SupervisorError(RuntimeError):
    """An agent could not be started or stopped, with a reason a user can act on."""


@dataclass(frozen=True)
class Status:
    """Whether an agent is watching, and how confident we are of that.

    `UNVERIFIED` is a real answer, not a failure. After the dashboard restarts
    we have a pid but no handle, and on some platforms we cannot prove the
    process behind it is still ours. Saying so is better than reporting
    `RUNNING` from a pid that a text editor now owns - the same instinct as
    "an unverified check is never a pass".
    """

    state: str
    pid: int = 0
    detail: str = ""

    @property
    def is_running(self) -> bool:
        return self.state in (RUNNING, UNVERIFIED)

    def as_json(self) -> dict:
        return {"state": self.state, "pid": self.pid, "detail": self.detail}


def log_path(agent: Agent) -> Path:
    return agent.root / ".sentinel" / LOG_NAME


def bot_log_path() -> Path:
    """The bot's log lives per-user, not per-repo: it serves whichever repo it
    is currently pointed at."""
    return home() / BOT_LOG_NAME


def _alive(pid: int) -> bool:
    """Whether any process holds this pid. Says nothing about whose it is."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process. Alive, but definitely not ours.
        return True
    except OSError:
        return False
    return True


def _looks_like_ours(pid: int, module: str, match: str = "") -> bool | None:
    """Whether pid is running `module` (and mentions `match`). None means we
    could not tell.

    Pids are reused, so adopting one on liveness alone would eventually have
    the dashboard reporting a stranger's process as a running agent.
    """
    if os.name == "nt":
        # No cheap, dependency-free way to read another process's command line
        # on Windows. Answer honestly rather than guess.
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return False
    line = result.stdout.strip()
    return module in line and (not match or match in line)


def _status_for(key: str, pid: int, module: str, match: str, noun: str) -> Status:
    """Whether one supervised process is alive, and how sure we are.

    Two sources of truth, in order: a handle we hold (exact, immune to pid
    reuse), then a remembered pid verified by its command line. The second is
    what makes a process started by a *different* Sentinel - an earlier
    dashboard, or the bot - visible here at all.
    """
    with _lock:
        process = _running.get(key)

    if process is not None:
        if process.poll() is None:
            return Status(RUNNING, process.pid)
        # It exited on its own - a bad config, or someone killed it.
        with _lock:
            _running.pop(key, None)
        return Status(STOPPED, 0, f"the {noun} exited with code {process.returncode}")

    if not _alive(pid):
        return Status(STOPPED)

    ours = _looks_like_ours(pid, module, match)
    if ours is True:
        return Status(RUNNING, pid, "started outside this dashboard")
    if ours is False:
        return Status(STOPPED, 0, f"the recorded process is no longer a Sentinel {noun}")
    return Status(UNVERIFIED, pid, "a process is alive but could not be confirmed as ours")


def status(agent: Agent) -> Status:
    """Is this agent watching right now?"""
    return _status_for(agent.id, agent.pid, _WATCHER_MODULE, str(agent.root), "watcher")


def bot_status(pid: int) -> Status:
    """Is the Telegram bot answering right now?"""
    return _status_for(_BOT_KEY, pid, _BOT_MODULE, "", "bot")


def _spawn(key: str, module: str, args: list[str], log_file: Path, noun: str) -> Status:
    """Start one supervised process, or explain why it would not start.

    Deliberately spawns the module's own command line entry point rather than
    calling into it, so the GUI and the terminal cannot drift into behaving
    differently and the CLI panel shows literally what the buttons run.
    """
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handle = log_file.open("a", encoding="utf-8")
    except OSError as exc:
        raise SupervisorError(f"Could not open the {noun} log at {log_file}: {exc}") from exc

    handle.write(f"\n--- started {datetime.now(timezone.utc).isoformat(timespec='seconds')} ---\n")
    handle.flush()

    try:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", module, *args],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # Deliberately no cwd override. Both entry points take absolute
            # paths, so their working directory is irrelevant - and setting it
            # to the watched repo once moved `sentinel` off the child's import
            # path, which failed silently as a process that started and
            # immediately died. `_child_environment` is what actually makes the
            # import work wherever the dashboard was launched from.
            env=_child_environment(),
            # Its own process group, so stopping one never signals the
            # dashboard, Ctrl-C in the dashboard's terminal does not take
            # everything down with it, and closing the dashboard leaves the
            # supervision you asked for still running.
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        handle.close()
        raise SupervisorError(f"Could not start the {noun}: {exc}") from exc
    finally:
        # Popen duplicated the descriptor; this copy is no longer needed.
        try:
            handle.close()
        except OSError:
            pass

    # Give it a moment to fall over. A process that dies on a bad config or a
    # broken import would otherwise be reported as running, and the person who
    # pressed Start would believe it was working when it was not.
    try:
        process.wait(timeout=_STARTUP_CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise SupervisorError(
            f"The {noun} stopped immediately (exit code {process.returncode}).\n"
            + (_last_error(log_file) or f"See {log_file} for what it printed.")
        )

    with _lock:
        _running[key] = process
    return Status(RUNNING, process.pid)


def start(agent: Agent) -> Status:
    """Start watching this agent's repo. Idempotent."""
    current = status(agent)
    if current.is_running:
        return current

    if not agent.root.is_dir():
        raise SupervisorError(f"{agent.root} is no longer a folder. Remove this agent or restore the path.")

    return _spawn(agent.id, _WATCHER_MODULE, [str(agent.root)], log_path(agent), "watcher")


def start_bot(pid: int = 0) -> Status:
    """Start the Telegram bot. Idempotent.

    Takes no repo: the bot reads which one it answers about from its own
    settings, so it can be pointed elsewhere while it runs.
    """
    current = bot_status(pid)
    if current.is_running:
        return current

    return _spawn(_BOT_KEY, _BOT_MODULE, [], bot_log_path(), "bot")


def _child_environment() -> dict:
    """The environment a watcher is started with.

    Sentinel is usually run from a checkout rather than installed, so the child
    only finds the package because the parent's working directory happened to
    contain it. Putting the package's own parent on PYTHONPATH makes a watcher
    start correctly no matter where the dashboard was launched from.
    """
    environment = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parent.parent)

    existing = environment.get("PYTHONPATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if package_parent not in parts:
        parts.insert(0, package_parent)
    environment["PYTHONPATH"] = os.pathsep.join(parts)
    return environment


def _last_error(log_file: Path) -> str:
    """The most useful line from a log, for a start that failed."""
    lines = [line.strip() for line in _tail_file(log_file, 20) if line.strip() and not line.startswith("---")]
    return lines[-1] if lines else ""


def _stop_process(key: str, pid: int, module: str, match: str) -> Status:
    """Stop one supervised process. Terminates politely, then insists."""
    with _lock:
        process = _running.pop(key, None)

    if process is not None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return Status(STOPPED)

    # Started by an earlier dashboard, or by the bot. Only signal it if we can
    # show it is ours - a reused pid could be anything.
    if _alive(pid) and _looks_like_ours(pid, module, match) is True:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise SupervisorError(f"Could not stop process {pid}: {exc}") from exc

    return Status(STOPPED)


def stop(agent: Agent) -> Status:
    """Stop watching this agent's repo."""
    return _stop_process(agent.id, agent.pid, _WATCHER_MODULE, str(agent.root))


def stop_bot(pid: int = 0) -> Status:
    """Stop the Telegram bot."""
    return _stop_process(_BOT_KEY, pid, _BOT_MODULE, "")


def running_here() -> int:
    """How many supervised processes this dashboard started and still holds.

    There is deliberately no `stop_all()`. Closing the dashboard must not stop
    the supervision you left running - that is the whole point of being able to
    ask from your phone. The caller says what is still alive instead.
    """
    with _lock:
        return sum(1 for process in _running.values() if process.poll() is None)


def _tail_file(log_file: Path, lines: int) -> list[str]:
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def tail(agent: Agent, lines: int = 200) -> list[str]:
    """The end of this agent's watcher log, for the UI to show."""
    return _tail_file(log_path(agent), lines)


def bot_tail(lines: int = 200) -> list[str]:
    """The end of the bot's log."""
    return _tail_file(bot_log_path(), lines)


# --- jobs -----------------------------------------------------------------
#
# The model-backed features are slow enough that a synchronous request would
# time out, so they run here and are polled.

# Every button in the UI, and the question it really asks. Routing through
# `query.answer()` keeps the GUI on the same seam as the bot and the CLI: it
# can restate a decided verdict, never reach past it into the orchestrator.
JOB_QUESTIONS = {
    "explain": "what did it just do?",
    "review": "can we ship?",
    "why": "why did you stop it?",
    "rules": "did it follow my rules?",
    "fix": "/fix",
}

DOCTOR = "doctor"
JOB_KINDS = tuple(JOB_QUESTIONS) + (DOCTOR,)

# Enough to keep a session's worth of answers, bounded so a long-running
# dashboard does not grow without limit.
_MAX_JOBS = 50

_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()


@dataclass
class Job:
    """One slow question, and its answer when it arrives."""

    id: str
    kind: str
    agent_id: str
    state: str = RUNNING
    output: str = ""
    error: str = ""
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finished: str = ""

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "agent_id": self.agent_id,
            "state": self.state,
            "output": self.output,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


def _run_job(job: Job, root: Path) -> None:
    try:
        if job.kind == DOCTOR:
            from .doctor import run as run_doctor

            output = "\n".join(check.describe() for check in run_doctor(root))
        else:
            from .interfaces.query import answer

            output = answer(JOB_QUESTIONS[job.kind], root).text
        state, error = "done", ""
    except Exception as exc:  # noqa: BLE001 - a failed job is reported, never raised into the server
        output, state, error = "", "failed", str(exc)

    with _jobs_lock:
        job.output, job.state, job.error = output, state, error
        job.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")


def submit(kind: str, agent: Agent) -> Job:
    """Start one slow question and return immediately."""
    if kind not in JOB_KINDS:
        raise SupervisorError(f"Unknown job '{kind}'. Expected one of: {', '.join(JOB_KINDS)}")

    job = Job(id=uuid.uuid4().hex[:12], kind=kind, agent_id=agent.id)
    with _jobs_lock:
        _jobs[job.id] = job
        for stale in sorted(_jobs.values(), key=lambda item: item.started)[: max(len(_jobs) - _MAX_JOBS, 0)]:
            _jobs.pop(stale.id, None)

    threading.Thread(target=_run_job, args=(job, agent.root), daemon=True).start()
    return job


def job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def reset_jobs() -> None:
    """Forget every job. For tests and a fresh process."""
    with _jobs_lock:
        _jobs.clear()
