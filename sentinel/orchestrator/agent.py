"""Puts Sentinel's verdict into plain English.

The model's job here is narrow on purpose: it restates an `Assessment` that has
already been decided. It does not weigh signals, invent findings, or change the
verdict. That decision is deterministic and lives in `verdict.py`.

Keeping the model out of the decision buys two things. The verdict is identical
on every run for identical input, which is what makes it trustworthy; and when
the model is unavailable, Sentinel still works — `explain()` falls back to the
plain report rather than failing (instructions.md #7).
"""

from ..llm import ModelSetupError, run_agent
from .verdict import Assessment

SYSTEM_PROMPT = """You are Sentinel, a supervisor watching an AI coding agent.

You will be given a verdict that has ALREADY been decided, with its reasons.
Restate it for a busy developer in 2-4 sentences.

Rules:
- Never change the verdict. Report the one you were given.
- Never invent a finding. Use only the reasons you were given.
- Lead with what the developer must do, then why.
- No preamble, no bullet points, no markdown. Plain sentences.
- If some checks could not be run, say so plainly - an unverified check is
  not a pass."""


def _prompt_for(assessment: Assessment) -> str:
    parts = [f"Verdict: {assessment.verdict.value}", f"Meaning: {assessment.headline}"]

    if assessment.reasons:
        parts.append("Reasons: " + "; ".join(assessment.reasons))
    if assessment.conditions:
        parts.append("Conditions to satisfy before shipping: " + "; ".join(assessment.conditions))
    if assessment.unchecked:
        parts.append("Checks that could not be run: " + "; ".join(assessment.unchecked))

    return "\n".join(parts)


def explain(assessment: Assessment) -> str:
    """Restate an assessment in plain English.

    Falls back to the deterministic report if the model is unreachable. The
    verdict is never lost just because the narration failed.
    """
    try:
        text = run_agent(SYSTEM_PROMPT, _prompt_for(assessment))
    except ModelSetupError as exc:
        return f"{assessment.report()}\n\n(No plain-English summary: {exc})"
    except Exception as exc:  # noqa: BLE001 - a demo must not die on a model hiccup
        return f"{assessment.report()}\n\n(No plain-English summary: the model call failed - {exc})"

    return text or assessment.report()
