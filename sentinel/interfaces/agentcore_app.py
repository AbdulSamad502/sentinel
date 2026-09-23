"""Sentinel's brain, hosted. The service Bedrock AgentCore Runtime runs.

AgentCore Runtime runs a container and speaks a small HTTP contract to it:
`POST /invocations` with a JSON payload, and `GET /ping` for health. That is
the whole interface, so this is a stdlib `http.server` rather than a new web
framework (instructions.md #4) - the same choice `dashboard.py` made.

**What this service is, and what it deliberately is not.** It takes a system
prompt and a prompt and returns text, running the same `run_agent()` every
local Sentinel runs, with `SENTINEL_PROVIDER=bedrock` inside the container. It
is a model, hosted. It is *not* the orchestrator: it never sees a session, it
never runs `synthesize()`, and there is no code path here that can produce a
verdict. That is what keeps the verdict identical whether Sentinel is talking
to a laptop's Ollama or to this - and it is the reason the LLM cannot decide a
verdict even once the LLM is on the other side of a network.

**The cost guard is not optional.** This endpoint spends a fixed AWS credit and
anyone who has the URL can call it. Prompt size, token count and call rate are
all capped here, at the door, before anything reaches Bedrock.

    python -m sentinel.interfaces.agentcore_app          # run it locally
    curl -s localhost:8080/ping
"""

import argparse
import json
import os
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..llm import AGENTCORE, BEDROCK, DEFAULT_MAX_TOKENS, ModelSetupError, provider, run_agent

# AgentCore's contract: the container listens here, and these two routes exist.
PORT = 8080
INVOCATIONS = "/invocations"
PING = "/ping"

# Bound to every interface on purpose, unlike `dashboard.py`, which must never
# be. This process is alone inside a container whose only door is the runtime's
# authorizer; binding to localhost there would simply make it unreachable.
HOST = "0.0.0.0"  # noqa: S104 - see above

# The cost guard. A diff big enough to exceed this is also a change too big for
# one narration to be worth reading.
MAX_PROMPT_CHARS = 60_000
MAX_SYSTEM_CHARS = 8_000
MAX_TOKENS_CEILING = DEFAULT_MAX_TOKENS

# Per install, per window. Generous for a person working, immediately in the
# way of anything hammering the endpoint.
RATE_LIMIT_CALLS = 30
RATE_LIMIT_WINDOW_SECONDS = 300

_calls: dict[str, deque] = defaultdict(deque)


class Refused(Exception):
    """The request is not going to Bedrock, and here is the status to send back."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _string(payload: dict, key: str, limit: int, required: bool = True) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise Refused(400, f"'{key}' must be a string.")
    value = value.strip()
    if required and not value:
        raise Refused(400, f"'{key}' is required.")
    if len(value) > limit:
        raise Refused(413, f"'{key}' is {len(value)} characters; the limit is {limit}.")
    return value


def _tokens(payload: dict) -> int:
    """The token budget, clamped rather than rejected.

    A caller asking for more than we will pay for gets our ceiling and an
    answer, not an error. The ceiling is the guard; the request is a hint.
    """
    value = payload.get("max_tokens", DEFAULT_MAX_TOKENS)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_MAX_TOKENS
    return min(value, MAX_TOKENS_CEILING)


def within_rate_limit(client: str, now: float | None = None) -> bool:
    """Whether this install may make another call. Prunes as it goes."""
    now = time.monotonic() if now is None else now
    seen = _calls[client]
    while seen and now - seen[0] > RATE_LIMIT_WINDOW_SECONDS:
        seen.popleft()
    if len(seen) >= RATE_LIMIT_CALLS:
        return False
    seen.append(now)
    return True


def reset_rate_limit() -> None:
    """Forget every recorded call. For tests and for a fresh process."""
    _calls.clear()


def check_provider() -> None:
    """Refuse to run as a proxy to ourselves.

    A container started with `SENTINEL_PROVIDER=agentcore` would call its own
    endpoint for every request. One misplaced environment variable is all that
    takes, and the failure looks like a hang rather than a mistake.
    """
    if provider() == AGENTCORE:
        raise ModelSetupError(
            "This service cannot run with SENTINEL_PROVIDER=agentcore - it would call itself.\n"
            f"Set SENTINEL_PROVIDER={BEDROCK} (or any other provider) in the runtime's environment."
        )


def handle(payload: object, now: float | None = None) -> tuple[int, dict]:
    """One request, answered. Returns the HTTP status and the JSON body.

    Never raises: a hosted service that dies on one bad request takes every
    other caller's answer with it (instructions.md #7).
    """
    try:
        if not isinstance(payload, dict):
            raise Refused(400, "The body must be a JSON object.")

        system = _string(payload, "system", MAX_SYSTEM_CHARS)
        prompt = _string(payload, "prompt", MAX_PROMPT_CHARS)
        client = _string(payload, "client", 128, required=False) or "anonymous"

        if not within_rate_limit(client, now):
            raise Refused(
                429,
                f"Rate limit reached: {RATE_LIMIT_CALLS} calls per "
                f"{RATE_LIMIT_WINDOW_SECONDS // 60} minutes per install.",
            )

        check_provider()
        return 200, {"text": run_agent(system, prompt, _tokens(payload))}
    except Refused as exc:
        return exc.status, {"error": str(exc)}
    except ModelSetupError as exc:
        # The runtime is misconfigured, which is ours to fix, not the caller's.
        return 500, {"error": f"The hosted runtime is not set up correctly: {exc}"}
    except Exception as exc:  # noqa: BLE001 - one failed call must not end the service
        return 502, {"error": f"The model call failed: {exc}"}


class Handler(BaseHTTPRequestHandler):
    """AgentCore's two routes, and nothing else."""

    server_version = "Sentinel"

    def do_GET(self) -> None:  # noqa: N802 - the base class names it this
        if self.path.split("?")[0] == PING:
            self._send(200, {"status": "Healthy"})
            return
        self._send(404, {"error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802 - the base class names it this
        if self.path.split("?")[0] != INVOCATIONS:
            self._send(404, {"error": "Not found."})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_PROMPT_CHARS + MAX_SYSTEM_CHARS + 4096:
            self._send(413, {"error": "Request too large."})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"Body is not valid JSON: {exc}"})
            return

        self._send(*handle(payload))

    def _send(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args) -> None:
        """Log the route and the status, never the body.

        Bodies here are other people's source code. The one thing this service
        must not do is write a customer's diff into a log we keep.
        """
        print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}".strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Sentinel's hosted model service.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", PORT)))
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args(argv)

    # Inside the runtime the model is Bedrock. Set here rather than relied on,
    # so a container missing one environment variable still starts correctly.
    os.environ.setdefault("SENTINEL_PROVIDER", BEDROCK)

    try:
        check_provider()
    except ModelSetupError as exc:
        print(exc)
        return 1

    print(f"Sentinel runtime on http://{args.host}:{args.port}  (provider: {provider()})")
    print(f"  POST {INVOCATIONS}   GET {PING}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
