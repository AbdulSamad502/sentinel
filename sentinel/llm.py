"""The one place Sentinel decides which LLM it talks to.

Every agent runs through `run_agent()`, and every model comes from
`get_model()`. Nothing else in the codebase imports a model provider or builds
a Strands `Agent`. That is what makes swapping providers a change to this file
alone, and it is the promise Phase 5 was designed around.

Three providers, chosen by `SENTINEL_PROVIDER`:

  * `ollama`    - a model on this laptop. The dev default: fast, free, private.
  * `bedrock`   - AWS Bedrock with this machine's AWS credentials.
  * `agentcore` - Sentinel's hosted brain on Bedrock AgentCore. No Ollama and
                  no AWS account needed on this machine; see `remote.py`.

Settings come from the environment first, then the `model` section of
Sentinel's own `sentinel.config.json`, then a default. Deliberately Sentinel's
own config and never a watched repo's: which provider Sentinel runs on is a
property of this *installation*, not a rule the watched project gets to set.
A repo we are supervising must not be able to redirect our model calls.

During local dev the team runs on different laptops with different models
pulled, so the Ollama model is chosen at runtime from whatever is actually
installed on this machine, and the choice is remembered per-machine.

**The provider never decides anything.** It narrates. `verdict.synthesize()`
stays deterministic no matter which of the three answered, and a test pins the
verdict as byte-identical across all of them.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from typing import TYPE_CHECKING

from .shared.config_file import DEFAULT_CONFIG_PATH, ConfigError, read_config, section

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    from strands.models.model import Model

DEFAULT_OLLAMA_HOST = "http://localhost:11434"

OLLAMA = "ollama"
BEDROCK = "bedrock"
AGENTCORE = "agentcore"
GROQ = "groq"
PROVIDERS = (OLLAMA, BEDROCK, AGENTCORE, GROQ)

# What docs/aws-setup.md tells the team to pick, and where Bedrock has the
# widest model availability. Overridden by SENTINEL_AWS_REGION or the config.
DEFAULT_AWS_REGION = "us-east-1"

# Where this machine's remembered model choice lives. Gitignored: it is a
# per-laptop preference, not a project setting.
_CHOICE_FILE = Path(__file__).resolve().parent.parent / ".sentinel" / "model"


class ModelSetupError(RuntimeError):
    """Raised when no usable model can be resolved.

    Carries a message the user can act on directly (instructions.md #7).
    """


def ollama_host() -> str:
    return os.environ.get("SENTINEL_OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")


def installed_models(host: str | None = None) -> list[str]:
    """Model ids currently pulled on this machine, chat-capable ones only.

    Uses Ollama's /api/tags over stdlib urllib — listing models is not worth a
    new dependency (instructions.md #4).
    """
    host = host or ollama_host()
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as response:
            payload = json.load(response)
    except urllib.error.URLError as exc:
        raise ModelSetupError(
            f"Could not reach Ollama at {host} ({exc.reason}).\n"
            "Start it with `ollama serve`, or point Sentinel elsewhere with "
            "SENTINEL_OLLAMA_HOST."
        ) from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise ModelSetupError(f"Ollama at {host} returned an unusable response: {exc}") from exc

    names = [model["name"] for model in payload.get("models", [])]
    # Embedding models can't hold a conversation, so don't offer them as a
    # choice. Name match is enough here — no need for anything cleverer.
    return sorted(name for name in names if "embed" not in name)


def _remembered_choice() -> str | None:
    try:
        choice = _CHOICE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return choice or None


def _remember_choice(model_id: str) -> None:
    try:
        _CHOICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CHOICE_FILE.write_text(model_id + "\n", encoding="utf-8")
    except OSError:
        # Not being able to save the preference is not worth failing a run over;
        # the user just gets asked again next time.
        pass


def _cannot_ask(models: list[str]) -> ModelSetupError:
    return ModelSetupError(
        "No model chosen, and there is no terminal to ask on.\n"
        f"Set SENTINEL_MODEL to one of: {', '.join(models)}"
    )


def _prompt_for_model(models: list[str]) -> str:
    print("\nWhich local model should Sentinel use?\n")
    for index, name in enumerate(models, start=1):
        print(f"  {index}. {name}")
    print()

    while True:
        try:
            answer = input(f"Pick 1-{len(models)}: ").strip()
        except EOFError as exc:
            # isatty() is not reliable across the shells the team uses, so this
            # is the guard that actually holds for piped/CI runs.
            raise _cannot_ask(models) from exc
        except KeyboardInterrupt as exc:
            raise ModelSetupError("Cancelled.") from exc

        if answer.isdigit() and 1 <= int(answer) <= len(models):
            return models[int(answer) - 1]
        print("Not one of the options - try again.")


def resolve_model_id() -> str:
    """Decide which model id to use, asking the user only when it must.

    Order: SENTINEL_MODEL env var, then this machine's remembered choice, then
    an interactive pick from what Ollama has installed.
    """
    from_env = os.environ.get("SENTINEL_MODEL", "").strip()
    if from_env:
        return from_env

    available = installed_models()
    if not available:
        raise ModelSetupError(
            f"Ollama at {ollama_host()} has no chat models installed.\n"
            "Pull one first, e.g. `ollama pull qwen3:4b`."
        )

    remembered = _remembered_choice()
    if remembered in available:
        return remembered

    if len(available) == 1:
        return available[0]

    if not sys.stdin.isatty():
        raise _cannot_ask(available)

    chosen = _prompt_for_model(available)
    _remember_choice(chosen)
    return chosen


# Reasoning models (qwen3 and friends) spend tokens thinking before they answer,
# and that thinking counts against the budget. Sentinel's replies are a few
# sentences, but the budget has to cover the thinking too or the model runs out
# mid-sentence and the answer is lost.
DEFAULT_MAX_TOKENS = 4096

# A ceiling on what one call may send, enforced for every provider in
# `run_agent`. It bounds both the spend and how much of a repo can leave the
# machine in a single prompt. `remote.py` re-checks it to save a round trip.
MAX_PROMPT_CHARS = 60_000


# --- settings -------------------------------------------------------------
#
# Environment first, then Sentinel's own config, then a default. The
# environment wins so a demo, a script or a container can pin a provider for
# one run without editing a file the whole team shares.


def _configured() -> dict:
    """The `model` section of Sentinel's own config file.

    A missing file is not an error - Sentinel installed as a package has no
    repo-root config, and the defaults are all it needs. A *malformed* one is,
    because silently falling back to a different provider than the file asks
    for is exactly the kind of quiet wrong answer this project exists to stop.
    """
    if not DEFAULT_CONFIG_PATH.is_file():
        return {}
    try:
        return section(read_config(DEFAULT_CONFIG_PATH), "model")
    except ConfigError as exc:
        raise ModelSetupError(f"Cannot tell which model provider to use: {exc}") from exc


def setting(name: str, env: str, fallback: str = "") -> str:
    """One model setting, resolved in precedence order."""
    from_env = os.environ.get(env, "").strip()
    if from_env:
        return from_env

    value = _configured().get(name, fallback)
    if not isinstance(value, str):
        raise ModelSetupError(f"'model.{name}' in {DEFAULT_CONFIG_PATH} must be a string, got {value!r}.")
    return value.strip() or fallback


def provider() -> str:
    """Which provider this installation talks to."""
    name = setting("provider", "SENTINEL_PROVIDER", OLLAMA).lower()
    if name not in PROVIDERS:
        raise ModelSetupError(
            f"Unknown model provider {name!r}.\n"
            f"Set SENTINEL_PROVIDER to one of: {', '.join(PROVIDERS)}"
        )
    return name


def aws_region() -> str:
    """The region for the AWS providers.

    Falls back to the AWS CLI's own variables, so a laptop already configured
    with `aws configure` needs no Sentinel-specific setting at all.
    """
    return (
        setting("aws_region", "SENTINEL_AWS_REGION")
        or os.environ.get("AWS_REGION", "").strip()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        or DEFAULT_AWS_REGION
    )


# --- the providers --------------------------------------------------------


def _ollama_model(max_tokens: int) -> "Model":
    try:
        from strands.models.ollama import OllamaModel
    except ImportError as exc:
        raise ModelSetupError(f"The Strands SDK is not installed ({exc}).\npip install -r requirements.txt") from exc
    return OllamaModel(host=ollama_host(), model_id=resolve_model_id(), max_tokens=max_tokens)


def _bedrock_model(max_tokens: int) -> "Model":
    try:
        from strands.models import BedrockModel
    except ImportError as exc:
        raise ModelSetupError(
            f"Bedrock support is not installed ({exc}).\npip install -r requirements.txt"
        ) from exc

    model_id = setting("bedrock_model_id", "SENTINEL_BEDROCK_MODEL")
    if not model_id:
        raise ModelSetupError(
            "No Bedrock model id configured.\n"
            "Set SENTINEL_BEDROCK_MODEL, or 'model.bedrock_model_id' in sentinel.config.json.\n"
            "Ids differ by account and region - list yours with:\n"
            "  aws bedrock list-foundation-models --region "
            f"{aws_region()} --query \"modelSummaries[].modelId\" --output table"
        )
    return BedrockModel(model_id=model_id, region_name=aws_region(), max_tokens=max_tokens)


# Groq speaks the OpenAI wire format, so Strands' OpenAI provider reaches it
# with nothing but a base_url. Going through Strands rather than raw HTTP is the
# point: it keeps every provider on one SDK, one message format and one seam.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def groq_api_key() -> str:
    """The Groq key. Environment only, deliberately.

    Every other model setting also reads `sentinel.config.json`, and that file
    is checked into git - the same reasoning as `remote._token()`. A secret has
    no business being offered a home there, and Sentinel's own
    `no-hardcoded-credentials` norm would flag it if it took one.
    """
    return os.environ.get("GROQ_API_KEY", "").strip()


def _groq_model(max_tokens: int) -> "Model":
    try:
        from strands.models import OpenAIModel
    except ImportError as exc:
        raise ModelSetupError(
            f"Groq support is not installed ({exc}).\npip install -r requirements.txt"
        ) from exc

    key = groq_api_key()
    if not key:
        raise ModelSetupError(
            "No Groq API key found.\n"
            "Set GROQ_API_KEY in your environment - not in sentinel.config.json, which is committed.\n"
            "Keys are at https://console.groq.com/keys"
        )

    # No default model id: ids change under you, and a stale default fails as a
    # confusing 404 at demo time. Sentinel's own `no-hardcoded-model-ids` norm
    # takes the same position.
    model_id = setting("groq_model_id", "SENTINEL_GROQ_MODEL")
    if not model_id:
        raise ModelSetupError(
            "No Groq model id configured.\n"
            "Set SENTINEL_GROQ_MODEL, or 'model.groq_model_id' in sentinel.config.json.\n"
            "Current ids are listed at https://console.groq.com/docs/models"
        )

    return OpenAIModel(
        client_args={"api_key": key, "base_url": GROQ_BASE_URL},
        model_id=model_id,
        params={"max_tokens": max_tokens},
    )


def get_model(max_tokens: int = DEFAULT_MAX_TOKENS) -> "Model":
    """The model every locally-run Sentinel agent runs on.

    `agentcore` has no local model by definition - the model lives in the
    hosted runtime - so it is refused here rather than silently answered with
    a different provider's model. Call `run_agent()`, which handles all three.
    """
    name = provider()
    if name == BEDROCK:
        return _bedrock_model(max_tokens)
    if name == GROQ:
        return _groq_model(max_tokens)
    if name == AGENTCORE:
        raise ModelSetupError(
            "SENTINEL_PROVIDER=agentcore has no local model: the model runs in the hosted runtime.\n"
            "Call sentinel.llm.run_agent() instead of get_model()."
        )
    return _ollama_model(max_tokens)


# Failures worth translating. botocore reports these as exception *class names*
# rather than anything typed, and the raw text ("An error occurred
# (AccessDeniedException) when calling the Converse operation") tells a user
# nothing about what to do next.
_AWS_REMEDIES = {
    "NoCredentialsError": "No AWS credentials found. Run `aws configure`, or see docs/aws-setup.md.",
    "NoRegionError": "No AWS region set. Run `aws configure`, or set SENTINEL_AWS_REGION.",
    "AccessDeniedException": (
        "AWS refused the call. Request access to this model in the Bedrock console "
        "(Model access), and check the IAM policy - see docs/aws-setup.md."
    ),
    "ValidationException": (
        "Bedrock rejected the request. The model id is usually the cause: ids differ by "
        "account and region. See docs/aws-setup.md step 7."
    ),
    "ResourceNotFoundException": "Bedrock does not have that model id in this region. See docs/aws-setup.md step 7.",
    "ThrottlingException": "Bedrock is throttling this account. Wait and retry, or request a quota increase.",
}


def actionable_error(exc: Exception) -> Exception:
    """An AWS failure a user can act on, or the original if we have nothing to add.

    Public because `remote.py` needs it too: a Bedrock call and an AgentCore
    invocation fail in the same botocore ways, and a user should get the same
    remedy either way.
    """
    remedy = _AWS_REMEDIES.get(type(exc).__name__)
    if remedy is None:
        # botocore wraps most service errors in ClientError, with the real name
        # only inside the response payload.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        remedy = _AWS_REMEDIES.get(code)
    return ModelSetupError(f"{remedy}\n({exc})") if remedy else exc


def run_agent(system_prompt: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Ask the model one question and return its text. The only agent seam.

    Every model-backed feature in Sentinel goes through this one function -
    narration, the norm judgement, the freeform answer - so which provider
    answers is decided here and nowhere else.

    Raises `ModelSetupError` for anything the user can fix, and lets a genuine
    model failure through as itself. Callers catch both and fall back to their
    deterministic answer: a model hiccup must never cost the answer.
    """
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ModelSetupError(
            f"That prompt is {len(prompt):,} characters and Sentinel caps one model call at "
            f"{MAX_PROMPT_CHARS:,}.\nNarrow the change - the deterministic verdict does not need the model."
        )

    name = provider()
    if name == AGENTCORE:
        from .remote import ask

        return ask(system_prompt, prompt, max_tokens)

    try:
        from strands import Agent
    except ImportError as exc:
        raise ModelSetupError(f"The Strands SDK is not installed ({exc}).\npip install -r requirements.txt") from exc

    model = get_model(max_tokens)
    try:
        # callback_handler=None stops Strands streaming the reply to stdout:
        # this returns text, and the caller decides where it goes.
        agent = Agent(model=model, system_prompt=system_prompt, callback_handler=None)
        response = agent(prompt)
    except ModelSetupError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised, translated where we can
        raise actionable_error(exc) from exc

    return str(response).strip()
