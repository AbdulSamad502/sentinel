"""Runs the Code Risk Analyzer against a real pull request on GitHub.

Kept out of the unittest suite deliberately: it needs the network, and a test
suite that fails because GitHub is slow teaches the team to ignore failures.
The offline coverage is in `test_code_risk.py` and `test_github.py`.

    python tests/live_pr_check.py psf/requests 6800
    python tests/live_pr_check.py psf/requests            # newest open PR

Set GITHUB_TOKEN to raise the rate limit from 60 to 5000 requests an hour.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.code_risk.analyzer import analyze_pull_request
from sentinel.code_risk.config import load_risk_config
from sentinel.shared.config_file import ConfigError
from sentinel.shared.github import GitHubClient, GitHubError, parse_repo


def newest_open_pull_request(client: GitHubClient, owner: str, repo: str) -> int:
    numbers = client.open_pull_request_numbers(owner, repo, limit=1)
    if not numbers:
        raise GitHubError(f"{owner}/{repo} has no open pull requests to check.")
    return numbers[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a real pull request for risk.")
    parser.add_argument("repo", help="owner/repo, e.g. psf/requests")
    parser.add_argument("number", nargs="?", type=int, help="PR number (default: newest open)")
    args = parser.parse_args(argv)

    try:
        owner, repo = parse_repo(args.repo)
        config = load_risk_config()
        client = GitHubClient()

        if not client.is_authenticated:
            print("No GITHUB_TOKEN set - limited to 60 requests an hour.\n")

        number = args.number or newest_open_pull_request(client, owner, repo)
        print(f"Analyzing {owner}/{repo} PR #{number}\n")

        risk = analyze_pull_request(client, owner, repo, number, config)
    except (GitHubError, ConfigError) as exc:
        print(f"Could not complete the analysis: {exc}", file=sys.stderr)
        return 1

    print(risk.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
