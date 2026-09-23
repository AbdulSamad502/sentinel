"""The one place Sentinel talks to GitHub.

Every component that needs GitHub data imports this client rather than calling
the API itself (instructions.md #4). That keeps auth, pagination, and rate-limit
handling in a single place instead of drifting apart across sub-agents.

Uses stdlib `urllib` — the API is plain JSON over HTTPS and does not justify a
new dependency.

A token is optional. Without one GitHub allows 60 requests an hour, which is
enough to try things out but not enough for a demo, so set GITHUB_TOKEN.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 15


class GitHubError(RuntimeError):
    """A GitHub call failed, with a message the user can act on.

    Sentinel degrades to "unable to verify X" rather than crashing
    (instructions.md #7), so callers are expected to catch this.
    """


@dataclass(frozen=True)
class ChangedFile:
    """One file changed by a pull request or commit."""

    path: str
    status: str
    additions: int = 0
    deletions: int = 0

    @property
    def total_changes(self) -> int:
        return self.additions + self.deletions


class GitHubClient:
    """Minimal read-only GitHub client.

    Read-only on purpose: Sentinel observes and reports, it does not act on the
    repository (instructions.md #6). There are no write methods here and none
    should be added without that being an explicit, discussed task.
    """

    def __init__(self, token: str | None = None, api_root: str = API_ROOT, timeout: int = DEFAULT_TIMEOUT):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout

    @property
    def is_authenticated(self) -> bool:
        return bool(self.token)

    def _get(self, path: str, params: dict | None = None) -> list | dict:
        url = f"{self.api_root}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(url)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", "sentinel-supervisor")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise GitHubError(self._explain(exc, url)) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"Could not reach GitHub ({exc.reason}). Check the network connection.") from exc
        except json.JSONDecodeError as exc:
            raise GitHubError(f"GitHub returned something that wasn't JSON for {url}: {exc}") from exc

    def _explain(self, exc: urllib.error.HTTPError, url: str) -> str:
        """Turn an HTTP error into something the user can act on."""
        if exc.code == 404:
            return f"Not found: {url}. Check the owner/repo/number, and that the repo isn't private."
        if exc.code in (401, 403):
            if exc.headers.get("X-RateLimit-Remaining") == "0":
                hint = (
                    "Set a GITHUB_TOKEN to raise the limit from 60 to 5000 requests an hour."
                    if not self.token
                    else "Wait for the limit to reset."
                )
                return f"GitHub rate limit reached. {hint}"
            if not self.token:
                return f"GitHub refused the request ({exc.code}). Set GITHUB_TOKEN if the repo is private."
            return f"GitHub refused the request ({exc.code}). Check the token's scopes."
        return f"GitHub returned HTTP {exc.code} for {url}."

    def _paged(self, path: str, params: dict | None = None, max_pages: int = 10) -> list:
        """Follow pagination up to a bound.

        Bounded deliberately: a pull request with thousands of files is itself a
        risk signal, and Sentinel should not spend a rate-limit budget walking it.
        """
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results: list = []

        for page in range(1, max_pages + 1):
            batch = self._get(path, {**params, "page": page})
            if not isinstance(batch, list):
                raise GitHubError(f"Expected a list from {path}, got {type(batch).__name__}.")
            results.extend(batch)
            if len(batch) < params["per_page"]:
                break

        return results

    @staticmethod
    def _to_changed_files(payload: list) -> list[ChangedFile]:
        return [
            ChangedFile(
                path=item.get("filename", ""),
                status=item.get("status", "modified"),
                additions=item.get("additions", 0),
                deletions=item.get("deletions", 0),
            )
            for item in payload
            if item.get("filename")
        ]

    def pull_request_files(self, owner: str, repo: str, number: int) -> list[ChangedFile]:
        """Files changed by a pull request."""
        return self._to_changed_files(self._paged(f"/repos/{owner}/{repo}/pulls/{number}/files"))

    def commit_files(self, owner: str, repo: str, sha: str) -> list[ChangedFile]:
        """Files changed by a single commit."""
        payload = self._get(f"/repos/{owner}/{repo}/commits/{sha}")
        if not isinstance(payload, dict):
            raise GitHubError(f"Expected commit data for {sha}, got a {type(payload).__name__}.")
        return self._to_changed_files(payload.get("files", []))

    def pull_request_head_sha(self, owner: str, repo: str, number: int) -> str:
        """The commit at the tip of a pull request - what CI actually ran on."""
        payload = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError(f"Expected pull request data for #{number}.")
        sha = payload.get("head", {}).get("sha", "")
        if not sha:
            raise GitHubError(f"Pull request #{number} has no head commit.")
        return sha

    def check_conclusions(self, owner: str, repo: str, ref: str) -> list[str]:
        """Raw conclusions of the CI checks on a commit.

        Returns GitHub's own words ('success', 'failure', 'in_progress', ...);
        interpreting them into a CIStatus is the orchestrator's job, so this
        module stays a thin transport layer. An empty list means no checks ran.
        """
        payload = self._get(f"/repos/{owner}/{repo}/commits/{ref}/check-runs")
        if not isinstance(payload, dict):
            raise GitHubError(f"Expected check-run data for {ref}.")

        conclusions = []
        for run in payload.get("check_runs", []):
            # A run that has not finished has conclusion=null but a status we
            # still need to see, or a pending PR would look like it had no CI.
            conclusions.append(run.get("conclusion") or run.get("status") or "unknown")
        return conclusions

    def open_pull_request_numbers(self, owner: str, repo: str, limit: int = 5) -> list[int]:
        """Numbers of the most recently updated open pull requests."""
        payload = self._get(f"/repos/{owner}/{repo}/pulls", {"state": "open", "per_page": limit})
        if not isinstance(payload, list):
            raise GitHubError(f"Expected a pull request list for {owner}/{repo}.")
        return [item["number"] for item in payload if "number" in item]

    def commit_count_for_path(self, owner: str, repo: str, path: str, limit: int = 30) -> int:
        """How many recent commits touched this file.

        The churn signal. A plain count, capped at `limit` — tasks.md is explicit
        that this stays a count and does not become a graph.
        """
        commits = self._get(
            f"/repos/{owner}/{repo}/commits",
            {"path": path, "per_page": limit},
        )
        if not isinstance(commits, list):
            raise GitHubError(f"Expected a commit list for {path}, got {type(commits).__name__}.")
        return len(commits)


def parse_repo(reference: str) -> tuple[str, str]:
    """Split 'owner/repo' (or a GitHub URL) into its parts."""
    cleaned = reference.strip().rstrip("/")
    if cleaned.startswith(("http://", "https://")):
        parts = urllib.parse.urlparse(cleaned).path.strip("/").split("/")
    else:
        parts = cleaned.split("/")

    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise GitHubError(f"'{reference}' is not an owner/repo reference, e.g. 'psf/requests'.")
    return parts[0], parts[1]
