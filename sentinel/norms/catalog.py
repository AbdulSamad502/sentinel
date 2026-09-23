"""The norms most projects want, offered as a starting point by the wizard.

Nothing here is applied automatically. The wizard shows these, you pick the
ones that match your project, and they are written into that project's
`sentinel.config.json` as data you can edit afterwards.

**Every regex here must be audited against a real repo before it ships.** This
repo has already been bitten once: the keyword `*admin*` scored
`admin/css/widgets.css` as permissions code, and nobody noticed until it ran
against django/django. A norm that cries wolf gets the whole feature ignored.

    python -m sentinel.norms.checker --audit /path/to/some/repo

Patterns are matched against **single added lines**. A rule that needs to see
two lines at once cannot be expressed here - make it a statement-only norm and
let the model judge it.

Known limit, accepted deliberately: a pattern cannot tell code from a comment,
so a newly added comment reading `never set verify = False` is reported. The
finding quotes the line, so a human dismisses it in a second, and every attempt
to narrow it further also stopped catching `verify=False  # local dev only`.
Precision here is worth less than not missing the real thing.
"""

from ..action_monitor.actions import Verdict
from .norms import Norm

# Places a rule about production code has no business firing. Deliberately a
# separate list from `code_risk.test_paths` in the config: that one answers
# "is this change tested?", this one answers "should this rule apply here?".
# They look alike today and may not stay that way.
TEST_LIKE_PATHS = [
    "test_*",
    "*_test.*",
    "tests/*",
    "*/tests/*",
    "spec/*",
    "*.spec.*",
    "*.test.*",
    "*/fixtures/*",
    "*/__mocks__/*",
    "*.md",
    "*.rst",
    "*.txt",
]


SECRETS_AND_SECURITY = [
    Norm(
        id="no-hardcoded-credentials",
        statement="Never commit a real API key, token, or password. Read them from the environment or a secrets manager.",
        severity=Verdict.BLOCKED,
        patterns=[
            # Provider key formats, which are unambiguous enough to block on.
            r"\bsk-[A-Za-z0-9]{20,}",
            # AWS documents its example keys as ending in EXAMPLE, and every
            # SDK ships thousands of them in fixture JSON. Auditing against
            # botocore turned up 15 such hits; excluding the documented
            # placeholder costs nothing and removes all of them.
            r"\bAKIA(?![0-9A-Z]*EXAMPLE\b)[0-9A-Z]{16}\b",
            r"\bghp_[A-Za-z0-9]{30,}",
            r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        ],
        except_paths=TEST_LIKE_PATHS,
    ),
    Norm(
        id="no-secrets-assigned-inline",
        statement="Secrets must not be assigned as string literals in source; load them from configuration.",
        patterns=[
            # A secret-ish name assigned a long literal. Requires 12+
            # characters, no spaces, and at least one digit.
            #
            # The digit is what makes this usable. Without it, auditing against
            # botocore reported five copies of `SECRET_KEY = 'aws_secret_access_key'`
            # - constants naming an environment variable, which is the correct
            # way to do this and the opposite of a hardcoded secret. Real keys
            # essentially always carry digits; env-var names essentially never do.
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)\s*[:=]\s*[\"'](?=[^\"']*[0-9])[^\"'\s]{12,}[\"']",
        ],
        except_paths=TEST_LIKE_PATHS,
    ),
    Norm(
        id="no-disabled-certificate-checks",
        statement="Never turn off TLS certificate verification. Fix the certificate instead.",
        severity=Verdict.BLOCKED,
        patterns=[
            # `verify=False` needs an HTTP call on the same line to count.
            # On its own it is far too generic: numpy has
            # `array_function_dispatch(_dispatcher, verify=False, module='numpy')`,
            # which has nothing to do with TLS. This norm is BLOCKED, so a
            # loose pattern here would have produced a false STOP - the single
            # most damaging thing a supervisor can do.
            r"(?i)\b(requests|httpx|aiohttp|session|client|urlopen|\.get|\.post|\.put|\.patch|\.delete|\.head|\.request)\b[^\n]*\bverify\s*=\s*False\b",
            r"\brejectUnauthorized\s*:\s*false\b",
            r"\bNODE_TLS_REJECT_UNAUTHORIZED\s*[=:]\s*[\"']?0",
            r"\bssl\._create_unverified_context\b",
            r"\bInsecureSkipVerify\s*:\s*true\b",
            r"(?i)\bcurl\b[^\n]*\s(-k|--insecure)\b",
        ],
        except_paths=TEST_LIKE_PATHS,
    ),
    Norm(
        id="no-wildcard-cors",
        statement="Do not open CORS to every origin. Name the origins that are allowed.",
        patterns=[
            r"(?i)access-control-allow-origin[\"']?\s*[:,=]\s*[\"']\*[\"']",
            r"(?i)\ballow_origins\s*=\s*\[\s*[\"']\*[\"']",
        ],
        except_paths=TEST_LIKE_PATHS,
    ),
]


AI_AND_MODELS = [
    Norm(
        id="no-hardcoded-model-ids",
        statement="Model identifiers belong in configuration, never inline in code.",
        patterns=[
            # Only inside a string literal, and only where a version number
            # makes it unmistakably a model id rather than prose.
            r"[\"'](gpt-[0-9][\w.\-]*)[\"']",
            r"[\"'](claude-[\w.\-]*[0-9][\w.\-]*)[\"']",
            r"[\"'](gemini-[0-9][\w.\-]*)[\"']",
            r"[\"'](llama-?[0-9][\w.\-]*)[\"']",
            r"[\"'](mistral-[\w.\-]+|mixtral-[\w.\-]+)[\"']",
        ],
        except_paths=TEST_LIKE_PATHS,
    ),
    Norm(
        id="no-inline-prompts",
        statement="System prompts belong in one named place, not scattered inline through the code.",
    ),
]


CODE_HYGIENE = [
    Norm(
        id="no-debug-output",
        statement="Take debugging statements out before the change is done.",
        patterns=[
            r"\bconsole\.log\s*\(",
            r"^\s*debugger\s*;?\s*$",
            r"\bbreakpoint\s*\(\s*\)",
            r"\bpdb\.set_trace\s*\(",
            r"\bdd\s*\(\s*\$",
        ],
        except_paths=TEST_LIKE_PATHS,
    ),
    Norm(
        id="no-unnamed-error-catching",
        # The statement says what the patterns actually detect. An earlier
        # draft called this "no silently swallowed errors", but the regex
        # matches every bare `except:`, including `except: raise` - auditing
        # against sqlalchemy turned up 27 of them, most perfectly legitimate
        # re-raises. A rule whose name overstates its evidence is how a checker
        # loses the reader's trust.
        statement="Do not catch errors without naming them. Use a bare `except:` or an empty catch block only with a stated reason.",
        patterns=[
            r"^\s*except\s*:\s*(#.*)?$",
            r"\bcatch\s*\([^)]*\)\s*\{\s*\}",
        ],
        except_paths=TEST_LIKE_PATHS,
    ),
    Norm(
        id="no-skipped-tests",
        statement="Do not disable a test to make a change pass.",
        patterns=[
            r"\b(it|test|describe)\.skip\s*\(",
            r"@(unittest\.)?skip\b",
            r"@pytest\.mark\.skip\b",
            r"\bt\.Skip\s*\(",
        ],
    ),
]


JUDGEMENT_CALLS = [
    Norm(
        id="ask-before-design-changes",
        statement="Do not make visual or user-experience design decisions alone. Propose them and wait for a human to choose.",
    ),
    Norm(
        id="ask-before-new-dependencies",
        statement="Do not add a new third-party dependency without asking first.",
    ),
    Norm(
        id="keep-public-api-stable",
        statement="Do not rename or change the signature of anything callers outside this module depend on.",
    ),
    Norm(
        id="stay-on-task",
        statement="Only change what the task asked for. Unrelated refactors, reformatting and cleanups need their own request.",
    ),
]


# Ready-made sets, so setting Sentinel up on a project is one answer rather
# than thirteen. Each names norms from the catalog above - a preset is a
# shortcut through the same rules, never a separate hidden set.
PRESETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "web": (
        "A web app or API",
        (
            "no-hardcoded-credentials",
            "no-secrets-assigned-inline",
            "no-disabled-certificate-checks",
            "no-wildcard-cors",
            "no-debug-output",
            "no-skipped-tests",
        ),
    ),
    "frontend": (
        "A frontend the team cares about the look of",
        (
            "no-hardcoded-credentials",
            "no-debug-output",
            "no-skipped-tests",
            "ask-before-design-changes",
            "ask-before-new-dependencies",
        ),
    ),
    "ai": (
        "Something that calls an LLM",
        (
            "no-hardcoded-credentials",
            "no-secrets-assigned-inline",
            "no-hardcoded-model-ids",
            "no-inline-prompts",
            "no-debug-output",
        ),
    ),
    "payments": (
        "Handles payments or anything regulated",
        (
            "no-hardcoded-credentials",
            "no-secrets-assigned-inline",
            "no-disabled-certificate-checks",
            "no-skipped-tests",
            "no-unnamed-error-catching",
            "keep-public-api-stable",
            "stay-on-task",
        ),
    ),
    "library": (
        "A library other code depends on",
        (
            "no-hardcoded-credentials",
            "no-debug-output",
            "no-skipped-tests",
            "keep-public-api-stable",
            "ask-before-new-dependencies",
        ),
    ),
}


def preset_norms(name: str) -> list[Norm]:
    """The norms a preset selects, in catalog order."""
    entry = PRESETS.get(name)
    if entry is None:
        return []
    wanted = set(entry[1])
    return [norm for norm in all_norms() if norm.id in wanted]


GROUPS: dict[str, list[Norm]] = {
    "Secrets and security": SECRETS_AND_SECURITY,
    "AI and model configuration": AI_AND_MODELS,
    "Code hygiene": CODE_HYGIENE,
    "Judgement calls (the model decides these)": JUDGEMENT_CALLS,
}


def all_norms() -> list[Norm]:
    """Every built-in norm, in the order the wizard offers them."""
    return [norm for group in GROUPS.values() for norm in group]


def by_id(norm_id: str) -> Norm | None:
    return next((norm for norm in all_norms() if norm.id == norm_id), None)
