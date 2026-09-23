"""Checking that Sentinel will actually work here, before you need it to.

Every other command assumes a working setup. This one checks it, and says what
to do about each thing it finds. It exists because the first thing a new
teammate - or a judge with a fresh clone - does is run something, and "it did
not work" with no explanation is where people give up.

    python -m sentinel.doctor <repo>

Exit status is 0 unless something is genuinely broken. A warning is not a
failure: Sentinel works without git hooks or a model, just with less to say.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from .action_monitor.config import load_config
from .action_monitor.githooks import HOOKS, is_ours
from .code_risk.churn import is_git_repo
from .norms.config import load_norms
from .session import open_session
from .shared.config_file import ConfigError, resolve_config_path

PASS = "ok"
WARN = "warn"
FAIL = "FAIL"

_MARKERS = {PASS: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}


@dataclass(frozen=True)
class Check:
    """One thing that was checked, and what to do if it is not right."""

    name: str
    status: str
    detail: str
    fix: str = ""

    def describe(self) -> str:
        line = f"{_MARKERS[self.status]} {self.name}: {self.detail}"
        return line + (f"\n         {self.fix}" if self.fix and self.status != PASS else "")


def check_config(root: Path, config_path: Path) -> list[Check]:
    """The policy file, and whether it parses."""
    where = "this project" if config_path.parent == root else "Sentinel's own defaults"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return [
            Check(
                "config",
                FAIL,
                str(exc),
                f"Fix {config_path}, or regenerate it with: python -m sentinel.norms.wizard {root}",
            )
        ]

    checks = [Check("config", PASS, f"{config_path} ({where})")]

    try:
        norms = load_norms(config_path)
    except ConfigError as exc:
        return checks + [Check("rules", FAIL, str(exc), "Fix the norms section, or remove it and re-run the wizard.")]

    if not norms:
        checks.append(
            Check(
                "rules",
                WARN,
                "no project rules set",
                f"Sentinel will not check anything project-specific. Set them: python -m sentinel.norms.wizard {root}",
            )
        )
    else:
        judged = sum(1 for norm in norms if not norm.is_deterministic)
        checks.append(
            Check("rules", PASS, f"{len(norms)} rule(s), {len(norms) - judged} checked by pattern, {judged} by model")
        )

    protected = len(config.protected_paths)
    checks.append(Check("protected paths", PASS if protected else WARN, f"{protected} pattern(s) configured"))
    return checks


def check_session(root: Path, config_path: Path) -> Check:
    try:
        log = open_session(root, config_path)
    except ConfigError as exc:
        return Check("session", FAIL, str(exc))

    if not log.exists:
        return Check(
            "session",
            WARN,
            "nothing recorded yet",
            f"Start watching before the agent works: python -m sentinel.action_monitor.watcher {root}",
        )

    touched = len(log.recorded())
    skipped = len(log.skipped())
    detail = f"started {log.started_at()}, {touched} file(s) touched"
    if skipped:
        detail += f", {skipped} not snapshotted"
    return Check("session", PASS, detail)


def check_hooks(root: Path) -> Check:
    hooks_dir = root / ".git" / "hooks"
    if not is_git_repo(root):
        return Check(
            "git hooks",
            WARN,
            "not a git repository",
            "Git commands cannot be reported here, and churn history is unavailable.",
        )

    installed = [hook for hook in HOOKS if (hooks_dir / hook).is_file() and is_ours(hooks_dir / hook)]
    if not installed:
        return Check(
            "git hooks",
            WARN,
            "not installed",
            f"A `git push --force` will go unreported: python -m sentinel.action_monitor.githooks --install {root}",
        )
    return Check("git hooks", PASS, f"{', '.join(installed)} installed")


def check_model() -> Check:
    """Whether a model can be reached. A warning, never a failure.

    Everything deterministic - the verdict, the pattern rules, the diff - works
    with no model at all, so this being down costs narration and the judged
    rules, not the tool.

    Which check runs depends on which provider this installation is on, so a
    developer on the hosted runtime is not told to start Ollama.
    """
    try:
        from .llm import AGENTCORE, BEDROCK, GROQ, ModelSetupError, provider
    except ImportError as exc:  # pragma: no cover - a broken install, not a state to test
        return Check("model", WARN, f"Sentinel's own modules will not import ({exc})", "pip install -r requirements.txt")

    try:
        name = provider()
    except ModelSetupError as exc:
        return Check("model", FAIL, str(exc).splitlines()[0], "Set SENTINEL_PROVIDER, or fix sentinel.config.json.")

    if name == BEDROCK:
        return _check_bedrock()
    if name == AGENTCORE:
        return _check_agentcore()
    if name == GROQ:
        return _check_groq()
    return _check_ollama()


def _check_groq() -> Check:
    """A key and a model id. No inference call, for the same reason as Bedrock."""
    from .llm import ModelSetupError, groq_api_key, setting

    try:
        from strands.models import OpenAIModel  # noqa: F401
    except ImportError as exc:
        return Check("model", WARN, f"Groq support is not installed ({exc})", "pip install -r requirements.txt")

    if not groq_api_key():
        return Check("model", FAIL, "GROQ_API_KEY is not set", "Export it; keys are at https://console.groq.com/keys")

    try:
        model_id = setting("groq_model_id", "SENTINEL_GROQ_MODEL")
    except ModelSetupError as exc:
        return Check("model", FAIL, str(exc).splitlines()[0], "Fix sentinel.config.json.")

    if not model_id:
        return Check("model", FAIL, "no Groq model id configured", "Set SENTINEL_GROQ_MODEL.")
    return Check("model", PASS, f"Groq {model_id} (key set)")


def _check_bedrock() -> Check:
    """AWS credentials and a model id. Deliberately no inference call.

    Checking Bedrock for real means paying for a token and waiting for it, on a
    command people run casually. Everything that is usually wrong - no
    credentials, no region, no model id - is visible without spending anything.
    """
    from .llm import ModelSetupError, aws_region, setting

    try:
        from strands.models import BedrockModel  # noqa: F401
    except ImportError as exc:
        return Check("model", WARN, f"Bedrock support is not installed ({exc})", "pip install -r requirements.txt")

    try:
        model_id = setting("bedrock_model_id", "SENTINEL_BEDROCK_MODEL")
    except ModelSetupError as exc:
        return Check("model", WARN, str(exc).splitlines()[0], "Fix the 'model' section of sentinel.config.json.")
    if not model_id:
        return Check("model", WARN, "no Bedrock model id configured", "export SENTINEL_BEDROCK_MODEL=<id>  # docs/aws-setup.md step 7")

    try:
        import boto3

        credentials = boto3.Session().get_credentials()
    except Exception as exc:  # noqa: BLE001 - any failure here is the same answer
        return Check("model", WARN, f"could not read AWS credentials ({exc})", "Run: aws configure  # docs/aws-setup.md step 6")

    if credentials is None:
        return Check("model", WARN, "no AWS credentials found", "Run: aws configure  # docs/aws-setup.md step 6")
    return Check("model", PASS, f"Bedrock {model_id} in {aws_region()}")


def _check_agentcore() -> Check:
    """The hosted runtime. Configured or not - reaching it costs a real call."""
    from .remote import endpoint, runtime_arn

    where = endpoint() or runtime_arn()
    if not where:
        return Check(
            "model",
            WARN,
            "provider is agentcore but no runtime is configured",
            "export SENTINEL_AGENTCORE_ENDPOINT=https://...  # docs/deploy-agentcore.md",
        )
    return Check("model", PASS, f"hosted runtime at {where}")


def _check_ollama() -> Check:
    from .llm import installed_models, ollama_host, resolve_model_id

    try:
        available = installed_models()
    except Exception:  # noqa: BLE001 - any failure here is the same answer
        # Deliberately not the exception text: it already carries its own
        # remedy, and printing both says the same thing twice.
        return Check("model", WARN, f"cannot reach Ollama at {ollama_host()}", "Start it with: ollama serve")

    if not available:
        return Check("model", WARN, f"no chat models at {ollama_host()}", "Pull one: ollama pull qwen3:4b")

    try:
        chosen = resolve_model_id()
    except Exception:  # noqa: BLE001 - interactive prompt cannot run here
        return Check(
            "model",
            WARN,
            f"{len(available)} models available but none chosen",
            f"Set one so scripts do not stop to ask: export SENTINEL_MODEL={available[0]}",
        )
    return Check("model", PASS, f"{chosen} at {ollama_host()}")


def check_agent_interface() -> Check:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return Check(
            "agent interface",
            WARN,
            "the MCP SDK is not installed",
            "Your coding agent cannot consult Sentinel: pip install -r requirements.txt",
        )
    return Check("agent interface", PASS, "MCP available for the coding agent to consult")


def run(root: Path, config_path: Path | None = None) -> list[Check]:
    """Every check, in the order a person would care about them."""
    config_path = config_path or resolve_config_path(root)
    checks = check_config(root, config_path)
    checks.append(check_session(root, config_path))
    checks.append(check_hooks(root))
    checks.append(check_model())
    checks.append(check_agent_interface())
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that Sentinel is set up correctly for a repo.")
    parser.add_argument("path", nargs="?", default=".", help="the repo to check (default: current)")
    parser.add_argument("--config", type=Path, default=None, help="path to sentinel.config.json")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    print(f"Checking Sentinel's setup for {root}\n")
    checks = run(root, args.config)
    for check in checks:
        print(check.describe())

    failed = [check for check in checks if check.status == FAIL]
    warned = [check for check in checks if check.status == WARN]

    print()
    if failed:
        print(f"{len(failed)} problem(s) to fix before Sentinel will work here.")
        return 1
    if warned:
        print(f"Ready, with {len(warned)} thing(s) Sentinel will not be able to tell you about.")
        return 0
    print("Everything checks out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
