"""Checking a change against the project's norms.

Two paths, and the difference between them is the point:

`check_patterns` is pure and deterministic. Same diff, same norms, same
findings, every time, with no model and no network.

`check_statements` asks the model about the norms no regex can express. It is
the one place a model looks at a change and reports something, so it is fenced
in hard: it may only say *whether* a norm was broken and quote evidence for it.
The severity comes from the human-authored config. A model that cites a file
which is not in the change, or a norm id that does not exist, is hallucinating
and its finding is dropped.

If the model cannot be reached, or answers with something unparseable, the
statement-only norms go into `unchecked` and the verdict is capped at REVIEW.
They are never quietly treated as satisfied (instructions.md #7).

Both scan **added lines only**. A change is not to blame for problems it
inherited, and blaming it for them is how a tool gets ignored.

    python -m sentinel.norms.checker --audit /path/to/repo
"""

import argparse
import json
import re
import sys
from pathlib import Path

from ..action_monitor.rules import matches
from ..action_monitor.tree import walk_files
from ..explainer.diff import DELETED, FileChange, diff_block, numbered_additions
from ..llm import ModelSetupError, run_agent
from ..shared.config_file import ConfigError, resolve_config_path
from .config import load_norms
from .norms import BY_MODEL, BY_PATTERN, Norm, NormFinding

# Enough of a line to identify it, without pasting a minified bundle into the
# terminal or, worse, a whole secret.
_EVIDENCE_LIMIT = 120

SYSTEM_PROMPT = """You are Sentinel, checking an AI coding agent's work against a project's own rules.

You will be given a list of rules and the diff of what the agent changed.
Report only rules the diff actually breaks.

Rules for your answer:
- Reply with a JSON array and nothing else. No prose, no markdown, no code fences.
- Each item must be {"norm_id": "...", "path": "...", "evidence": "..."}.
- "path" must be one of the changed files you were shown. Never name another file.
- "evidence" must quote a line that appears in the diff.
- Judge only what the diff shows. If you are not sure a rule was broken, leave it out.
- Do not judge how serious a violation is. That is decided elsewhere.
- If no rule was broken, reply with exactly: []"""


def applies(norm: Norm, path: str) -> bool:
    """Whether this norm has anything to say about this file."""
    if norm.except_paths and any(matches(path, pattern) for pattern in norm.except_paths):
        return False
    if norm.applies_to:
        return any(matches(path, pattern) for pattern in norm.applies_to)
    return True


def check_patterns(changes: list[FileChange], norms: list[Norm]) -> list[NormFinding]:
    """Every regex-checkable norm the change breaks. Pure and deterministic.

    Patterns are matched one added line at a time, so a rule needing to see two
    lines at once cannot be written as a pattern. That is a deliberate limit:
    single-line matching is predictable, and anything subtler belongs in a
    statement for the model to judge.
    """
    compiled = [
        (norm, [re.compile(pattern) for pattern in norm.patterns]) for norm in norms if norm.is_deterministic
    ]
    if not compiled:
        return []

    findings: list[NormFinding] = []
    for change in changes:
        if change.status == DELETED:
            # Nothing was added, so there is nothing new to be at fault for.
            continue

        additions = numbered_additions(change)
        for norm, expressions in compiled:
            if not applies(norm, change.path):
                continue
            for line_number, text in additions:
                match = next((expression for expression in expressions if expression.search(text)), None)
                if match is None:
                    continue
                findings.append(
                    NormFinding(
                        norm_id=norm.id,
                        statement=norm.statement,
                        path=change.path,
                        severity=norm.severity,
                        evidence=_trim(text),
                        source=BY_PATTERN,
                        line=line_number,
                    )
                )
                # One finding per norm per file. Ten identical console.log hits
                # are one thing to fix, not ten lines of noise.
                break

    return findings


def check_statements(
    changes: list[FileChange],
    norms: list[Norm],
    max_diff_lines: int = 120,
) -> tuple[list[NormFinding], list[str]]:
    """Ask the model about the norms no regex can express.

    Returns `(findings, unchecked)`. One model call for the whole session, not
    one per norm per file - that is where the token saving comes from.
    """
    judged = [norm for norm in norms if not norm.is_deterministic]
    if not judged or not changes:
        return [], []

    names = ", ".join(norm.id for norm in judged)

    def cannot_check(why: str) -> tuple[list[NormFinding], list[str]]:
        return [], [f"the project norms that need judgement ({names}) - {why}"]

    # Everything from here on is inside the guard, prompt building included.
    # Nothing about checking a norm is worth crashing the orchestrator for
    # (instructions.md #7), and the caller treats an empty result as "clean" -
    # so a failure that escaped would read as a pass.
    try:
        prompt = statement_prompt(changes, judged, max_diff_lines)
        response = run_agent(SYSTEM_PROMPT, prompt)
    except ModelSetupError as exc:
        return cannot_check(str(exc))
    except Exception as exc:  # noqa: BLE001 - a demo must not die on a model hiccup
        return cannot_check(f"the model call failed ({exc})")

    reported = _parse_findings(response)
    if reported is None:
        return cannot_check("the model's answer could not be read")

    return _verify(reported, judged, changes), []


def statement_prompt(changes: list[FileChange], norms: list[Norm], max_diff_lines: int) -> str:
    """What the model is shown. Built here so it can be tested without a model."""
    parts = ["Rules:"]
    parts += [f"- {norm.id}: {norm.statement}" for norm in norms]
    parts.append("")
    parts.append("Changed files: " + ", ".join(change.path for change in changes))
    parts.append("")
    parts.append("Diff:")
    parts += [diff_block(change, max_diff_lines) for change in changes]
    return "\n".join(parts)


def _parse_findings(response: str) -> list[dict] | None:
    """The model's JSON array, or None if it did not send one.

    Tolerates a code fence, because small local models add them however firmly
    the prompt says not to. Tolerates nothing else.
    """
    text = response.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, list):
        return None
    return [item for item in parsed if isinstance(item, dict)]


def _verify(reported: list[dict], norms: list[Norm], changes: list[FileChange]) -> list[NormFinding]:
    """Keep only the findings that check out against the change we actually saw.

    A model naming a file that was never touched, or a rule that does not
    exist, has invented it. Dropping those silently is right: they are noise,
    not signal, and a supervisor that repeats a hallucination is worse than one
    that says nothing.
    """
    by_id = {norm.id: norm for norm in norms}
    changed_paths = {change.path for change in changes}
    findings: list[NormFinding] = []
    seen: set[tuple[str, str]] = set()

    for item in reported:
        norm = by_id.get(str(item.get("norm_id", "")))
        path = str(item.get("path", ""))
        if norm is None or path not in changed_paths:
            continue
        if (norm.id, path) in seen or not applies(norm, path):
            continue
        seen.add((norm.id, path))

        findings.append(
            NormFinding(
                norm_id=norm.id,
                statement=norm.statement,
                path=path,
                severity=norm.severity,
                evidence=_trim(str(item.get("evidence", "")).strip()) or "no line quoted",
                source=BY_MODEL,
            )
        )

    return findings


def correction_request(findings: list[NormFinding]) -> str:
    """A message you can paste to the coding agent, asking it to put this right.

    Sentinel writes the request; a human sends it. Sentinel does not talk to
    the coding agent, edit the code, or revert anything — it observes, analyses
    and reports, and acting on what it reports is the human's call
    (instructions.md #6).
    """
    if not findings:
        return "Nothing to correct - no norms were broken."

    lines = [
        "Your change breaks rules this project has set. Please fix these, and",
        "do not change anything else while you are in there:",
        "",
    ]
    for finding in findings:
        where = f"{finding.path}:{finding.line}" if finding.line else finding.path
        lines.append(f"- {where}")
        lines.append(f"    rule: {finding.statement}")
        lines.append(f"    what you wrote: {finding.evidence}")
        lines.append("")

    lines.append("Tell me how you fixed each one when you are done.")
    return "\n".join(lines)


def _trim(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _EVIDENCE_LIMIT else text[:_EVIDENCE_LIMIT] + "..."


# -- the audit tool ------------------------------------------------------
#
# Not part of the verdict path. This exists so a new pattern can be proved
# against a real repo before anyone trusts it, which is the check that would
# have caught the `*admin*` false positive before it shipped.


def audit(root: Path, norms: list[Norm]) -> list[tuple[str, int, str, str]]:
    """Run every pattern over every line of a real repo, for false positives.

    Deliberately scans whole files rather than added lines: the question here
    is "how noisy is this pattern?", and every existing line is a fair test.
    """
    compiled = [(norm, [re.compile(p) for p in norm.patterns]) for norm in norms if norm.is_deterministic]
    hits: list[tuple[str, int, str, str]] = []

    for absolute, relative in walk_files(root):
        try:
            if absolute.stat().st_size > 400_000:
                continue
            content = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for number, line in enumerate(content.splitlines(), start=1):
            for norm, expressions in compiled:
                if not applies(norm, relative):
                    continue
                if any(expression.search(line) for expression in expressions):
                    hits.append((norm.id, number, relative, _trim(line)))

    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit norm patterns against a real repo, to find false positives.")
    parser.add_argument("--audit", metavar="REPO", required=True, help="repo to scan")
    parser.add_argument("--config", type=Path, default=None, help="path to sentinel.config.json")
    parser.add_argument("--builtin", action="store_true", help="audit the built-in catalog instead of a config")
    args = parser.parse_args(argv)

    root = Path(args.audit).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    if args.builtin:
        from .catalog import all_norms

        norms = all_norms()
    else:
        try:
            norms = load_norms(args.config or resolve_config_path(root))
        except ConfigError as exc:
            print(f"\nCannot audit: {exc}\n", file=sys.stderr)
            return 1

    hits = audit(root, norms)
    if not hits:
        print(f"No pattern matched anything in {root}.")
        return 0

    counts: dict[str, int] = {}
    for norm_id, _, _, _ in hits:
        counts[norm_id] = counts.get(norm_id, 0) + 1

    print(f"{len(hits)} match(es) in {root}\n")
    for norm_id, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"{norm_id}: {count}")
        for hit_id, number, path, line in hits:
            if hit_id == norm_id:
                print(f"    {path}:{number}  {line}")
        print()

    print("Read these. Any pattern matching things it should not is a false")
    print("positive - narrow it, or make it a statement-only norm instead.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
