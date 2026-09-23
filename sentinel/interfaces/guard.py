"""What stops a web page from driving your dashboard.

While the dashboard was read-only, binding to `127.0.0.1` was enough of a
boundary. It is not any more. The control routes spawn processes and write
config files into real repos, and **localhost is not a security boundary in a
browser**: any page a person has open in another tab can issue requests to
`http://127.0.0.1:8765`. Without something here, a visited web page could
register agents, rewrite a project's rules, or start processes on the machine.

Four checks, each closing a different route in:

1. **A session token.** Minted per server start, printed in the terminal, and
   injected into the page we serve. Every mutating request must carry it in a
   header. A cross-origin page cannot read our HTML, so it cannot learn it.
2. **Origin rejection.** A browser attaches `Origin` on cross-site requests and
   scripts cannot forge it. Anything not ours is refused.
3. **A JSON content type.** An HTML form can POST cross-origin without CORS,
   but it cannot set `Content-Type: application/json`. Requiring it removes the
   one CSRF path that needs no JavaScript at all.
4. **Path confinement.** Every filesystem path arriving over the network is
   resolved and checked before anything is read or written.

The token is a CSRF defence, not authentication. Anything that can already read
this machine's memory or terminal has it - and if that is true, the dashboard
is not the problem.
"""

import ipaddress
import os
import secrets
import urllib.parse
from pathlib import Path

TOKEN_HEADER = "X-Sentinel-Token"
TOKEN_META = "sentinel-token"
JSON_CONTENT_TYPE = "application/json"

# Nothing sensitive is derived from this, but it is the only thing standing
# between a stray web page and a subprocess, so it gets real entropy.
_TOKEN_BYTES = 32

_token = ""


class Refused(Exception):
    """The request is not being served, and here is the status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def token() -> str:
    """This server's session token, minted on first use."""
    global _token
    if not _token:
        _token = secrets.token_urlsafe(_TOKEN_BYTES)
    return _token


def reset_token() -> str:
    """Mint a fresh token. For tests, and for a restarted server."""
    global _token
    _token = ""
    return token()


def _host_is_local(host: str) -> bool:
    """Whether an Origin's host is this machine.

    Accepts `localhost` and any loopback address, because people reach their
    own dashboard by several names and a browser sends back whichever was
    typed. Anything else is somebody else's site.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_origin(origin: str) -> None:
    """Refuse a request that came from another site.

    An absent Origin is allowed: `curl` and the CLI do not send one, and a
    browser always does on the cross-site requests that matter here.
    """
    if not origin:
        return
    if origin == "null":
        raise Refused(403, "Requests from an opaque origin are refused.")

    parsed = urllib.parse.urlparse(origin)
    if not _host_is_local(parsed.hostname or ""):
        raise Refused(403, "This dashboard only accepts requests from itself.")


def check_token(supplied: str) -> None:
    if not supplied:
        raise Refused(401, f"This request needs the {TOKEN_HEADER} header. Reload the dashboard.")
    # Constant time, so a wrong token cannot be found one character at a time.
    if not secrets.compare_digest(supplied, token()):
        raise Refused(403, "That token is not this session's. Reload the dashboard.")


def check_content_type(content_type: str) -> None:
    """Require JSON on mutating routes.

    This is the check that stops a plain HTML form, which is the only way to
    make a cross-origin POST without JavaScript.
    """
    if content_type.split(";")[0].strip().lower() != JSON_CONTENT_TYPE:
        raise Refused(415, f"Mutating requests must be {JSON_CONTENT_TYPE}.")


def check_mutating(headers) -> None:
    """Every check a mutating request has to pass."""
    check_origin(headers.get("Origin", ""))
    check_token(headers.get(TOKEN_HEADER, ""))
    check_content_type(headers.get("Content-Type", ""))


def check_reading(headers) -> None:
    """What a route that only reads has to pass.

    The filesystem browser is a read, but it exposes the shape of someone's
    disk, so it is held to the same origin and token rules as a write. Only the
    content type is dropped, because a GET has no body.
    """
    check_origin(headers.get("Origin", ""))
    check_token(headers.get(TOKEN_HEADER, ""))


def safe_directory(raw: str, must_exist: bool = True) -> Path:
    """A directory path from the network, resolved and checked.

    Resolving first is what matters: `..` and symlinks are collapsed before any
    decision is made about the result, so a path cannot mean one thing when
    checked and another when used.
    """
    text = (raw or "").strip()
    if not text:
        return Path.home()

    if "\x00" in text:
        raise Refused(400, "That path is not valid.")

    try:
        path = Path(text).expanduser().resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop
        raise Refused(400, f"That path cannot be resolved: {exc}")

    if must_exist and not path.is_dir():
        raise Refused(404, f"There is no folder at {path}.")
    return path


def inject_token(html: str) -> str:
    """Put this session's token into the page we serve.

    The dashboard reads it from the meta tag on load. It is only ever placed in
    a document served from loopback, and no other origin can read that
    document, which is what makes this safe to embed.
    """
    tag = f'<meta name="{TOKEN_META}" content="{token()}">'
    if "</head>" in html:
        return html.replace("</head>", f"  {tag}\n</head>", 1)
    return tag + html


def is_loopback_client(address: str) -> bool:
    """Whether a connecting peer is on this machine.

    Belt and braces: the server binds to loopback already, so this should never
    fail. It exists so that a future change to the bind address cannot quietly
    turn the control routes into a network service.
    """
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def print_banner(url: str) -> None:
    """Tell the terminal where the dashboard is, with its token."""
    print(f"  {url}?token={token()}")
    if os.environ.get("SENTINEL_PRINT_TOKEN", "").strip():
        print(f"  token: {token()}")
