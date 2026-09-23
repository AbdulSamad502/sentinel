"""Talking to Sentinel's hosted brain on Bedrock AgentCore.

This is what makes a zero-setup install possible. A developer who installs
Sentinel has no Ollama and no AWS account, but Sentinel still needs a model to
narrate a change or judge a written rule. `SENTINEL_PROVIDER=agentcore` points
those calls at a runtime we host, and nothing else about Sentinel changes.

Two transports, because the auth story differs:

  * **endpoint** - an HTTPS URL, called with an optional bearer token. This is
    the shipped app's path: AgentCore's own JWT-authorised endpoint, or a thin
    proxy in front of it. Stdlib urllib, no new dependency.
  * **arn** - the runtime's ARN, invoked with this machine's AWS credentials
    through boto3 and SigV4. This is the developer path: no token to mint, and
    it is the one to use while testing a deployment.

Both are configured in `llm.py`'s settings (env first, then the `model`
section). If both are set the endpoint wins, because it is the more explicit of
the two.

**What crosses the network, and what does not.** The runtime is sent a system
prompt and a prompt, and returns text. It is never sent raw signals and never
asked for a verdict - `verdict.synthesize()` runs on this machine, on data that
never left it, and stays deterministic. The hosted model narrates, exactly as
the local one does. That is why swapping providers cannot change a verdict.

Diffs *do* cross the network on this provider, which is a real privacy
trade-off and is stated plainly in the README rather than buried: a developer
who does not want that runs `SENTINEL_PROVIDER=ollama` and nothing leaves the
machine.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .llm import MAX_PROMPT_CHARS, ModelSetupError, actionable_error, aws_region, setting

# A hosted model on a cold runtime can take a while to answer. Long enough to
# survive a cold start, short enough that a demo does not appear to hang.
TIMEOUT_SECONDS = 180

# The runtime charges real money against a fixed credit, so the client refuses
# to send something absurd before the server has to. `run_agent` already applies
# this same cap for every provider; re-checking here saves the round trip for a
# caller that reaches `ask()` directly, and the server enforces its own limits.

# Where this machine's install id lives, next to the remembered model choice.
# Gitignored, and a random id rather than anything about the user: it exists so
# the hosted runtime can rate-limit per install without knowing who anyone is.
_INSTALL_FILE = Path(__file__).resolve().parent.parent / ".sentinel" / "install"


def install_id() -> str:
    """A stable random id for this installation, created on first use."""
    try:
        existing = _INSTALL_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    fresh = uuid.uuid4().hex
    try:
        _INSTALL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _INSTALL_FILE.write_text(fresh + "\n", encoding="utf-8")
    except OSError:
        # Not worth failing a call over. A new id per run only means the
        # rate limit is a little less precise.
        pass
    return fresh


def endpoint() -> str:
    return setting("agentcore_endpoint", "SENTINEL_AGENTCORE_ENDPOINT")


def runtime_arn() -> str:
    return setting("agentcore_arn", "SENTINEL_AGENTCORE_ARN")


def _token() -> str:
    """The bearer token, if there is one. Never logged, never in an error.

    Environment only, deliberately: every other model setting also reads from
    `sentinel.config.json`, and that file is checked into git. A secret has no
    business being offered a home there.
    """
    return os.environ.get("SENTINEL_AGENTCORE_TOKEN", "").strip()


def _not_configured() -> ModelSetupError:
    return ModelSetupError(
        "SENTINEL_PROVIDER=agentcore, but no hosted runtime is configured.\n"
        "Set one of:\n"
        "  SENTINEL_AGENTCORE_ENDPOINT=https://...   (a URL, with SENTINEL_AGENTCORE_TOKEN if it needs one)\n"
        "  SENTINEL_AGENTCORE_ARN=arn:aws:bedrock-agentcore:...   (uses this machine's AWS credentials)\n"
        "See docs/deploy-agentcore.md. To run locally instead: SENTINEL_PROVIDER=ollama"
    )


def payload_for(system_prompt: str, prompt: str, max_tokens: int) -> dict:
    """Exactly what the runtime is sent. Built here so it can be tested."""
    return {
        "system": system_prompt,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "client": install_id(),
    }


def ask(system_prompt: str, prompt: str, max_tokens: int) -> str:
    """Ask the hosted runtime one question and return its text."""
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ModelSetupError(
            f"That prompt is {len(prompt):,} characters and the hosted runtime accepts "
            f"{MAX_PROMPT_CHARS:,}.\nNarrow the change, or run a local model: SENTINEL_PROVIDER=ollama"
        )

    body = payload_for(system_prompt, prompt, max_tokens)
    url, arn = endpoint(), runtime_arn()
    if url:
        return _read(_over_https(url, body))
    if arn:
        return _read(_over_boto(arn, body))
    raise _not_configured()


def _read(answer: dict) -> str:
    """The text out of a runtime response, or the runtime's own error."""
    if not isinstance(answer, dict):
        raise RuntimeError("The hosted runtime returned something that was not a JSON object.")
    if answer.get("error"):
        raise RuntimeError(f"The hosted runtime refused: {answer['error']}")
    text = str(answer.get("text", "")).strip()
    if not text:
        raise RuntimeError("The hosted runtime returned an empty answer.")
    return text


def _over_https(url: str, body: dict) -> dict:
    """POST to the runtime's URL. Stdlib only (instructions.md #4)."""
    scheme = urllib.parse.urlparse(url).scheme
    host = urllib.parse.urlparse(url).hostname or ""
    if scheme != "https" and host not in ("localhost", "127.0.0.1"):
        # A bearer token and a private repo's diff over plaintext is not a
        # trade worth making for anyone's convenience.
        raise ModelSetupError(
            f"Refusing to send a diff and a token over {scheme or 'an unknown scheme'} to {host or url}.\n"
            "SENTINEL_AGENTCORE_ENDPOINT must be https:// (localhost may be http for testing)."
        )

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    token = _token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _http_failure(exc) from exc
    except urllib.error.URLError as exc:
        raise ModelSetupError(
            f"Could not reach the hosted runtime at {url} ({exc.reason}).\n"
            "Check the endpoint, or run a local model instead: SENTINEL_PROVIDER=ollama"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"The hosted runtime did not return JSON: {exc}") from exc


def _http_failure(exc: urllib.error.HTTPError) -> Exception:
    """An HTTP error a user can act on. Never quotes the request, so never the token."""
    if exc.code in (401, 403):
        return ModelSetupError(
            "The hosted runtime rejected these credentials (HTTP "
            f"{exc.code}).\nCheck SENTINEL_AGENTCORE_TOKEN, or run locally: SENTINEL_PROVIDER=ollama"
        )
    if exc.code == 429:
        return ModelSetupError(
            "The hosted runtime is rate-limiting this install (HTTP 429).\n"
            "Wait a minute, or run a local model: SENTINEL_PROVIDER=ollama"
        )
    return RuntimeError(f"The hosted runtime returned HTTP {exc.code}.")


def _over_boto(arn: str, body: dict) -> dict:
    """Invoke the runtime by ARN, signed with this machine's AWS credentials."""
    try:
        import boto3
    except ImportError as exc:
        raise ModelSetupError(
            f"Invoking a runtime by ARN needs boto3 ({exc}).\n"
            "pip install boto3, or use SENTINEL_AGENTCORE_ENDPOINT instead."
        ) from exc

    try:
        client = boto3.client("bedrock-agentcore", region_name=aws_region())
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            # AgentCore wants a session id of at least 33 characters. Sentinel's
            # calls carry their own context and share nothing between them, so a
            # fresh session per call is the honest thing to send.
            runtimeSessionId=uuid.uuid4().hex + uuid.uuid4().hex,
            payload=json.dumps(body).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
    except Exception as exc:  # noqa: BLE001 - translated where we can
        raise actionable_error(exc) from exc

    raw = response.get("response")
    text = raw.read() if hasattr(raw, "read") else raw
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"The hosted runtime did not return JSON: {exc}") from exc
