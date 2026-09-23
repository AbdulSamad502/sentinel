"""Setting Sentinel up on a project, by asking what the rules are.

This is the moment a developer hands a repo over: before the coding agent
touches anything, Sentinel asks what this project expects of it. Common rules
come from the catalog; the ones that matter most are usually the ones only the
team knows, so there is a free-text step for those.

The result is written to that repo's own `sentinel.config.json` as data. It can
be edited by hand afterwards, and it travels with the project.

    python -m sentinel.norms.wizard /path/to/repo

Nothing here is clever about terminals. `input()` is wrapped everywhere because
`sys.stdin.isatty()` already misled this team once - it returned True under
redirected stdin on Git Bash - so `EOFError` is the real guard (see llm.py).
"""

import argparse
import json
import sys
from pathlib import Path

from ..action_monitor.actions import Verdict
from ..shared.config_file import CONFIG_FILENAME, ConfigError, read_config
from . import catalog
from .norms import Norm


class Cancelled(RuntimeError):
    """The user backed out. Not an error - nothing should be written."""


def _ask(question: str) -> str:
    try:
        return input(question).strip()
    except EOFError as exc:
        raise Cancelled("no more input") from exc
    except KeyboardInterrupt as exc:
        raise Cancelled("interrupted") from exc


def _ask_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = _ask(question + suffix).lower()
    if not answer:
        return default
    return answer.startswith("y")


def choose_preset() -> list[Norm] | None:
    """Offer the ready-made sets first. None means "let me pick individually".

    Most people setting a tool up want a sensible default, not thirteen
    yes/no questions. A preset is a shortcut through the same catalog, so
    anything it selects can still be edited in the file afterwards.
    """
    keys = list(catalog.PRESETS)

    print("\nWhat kind of project is this? Sentinel will start from a matching set of rules.\n")
    for index, key in enumerate(keys, start=1):
        label, ids = catalog.PRESETS[key]
        print(f"   {index}. {label} ({len(ids)} rules)")
    print(f"   {len(keys) + 1}. Let me choose each rule myself")
    print()

    answer = _ask("Choose [1-%d]: " % (len(keys) + 1))
    if answer.isdigit() and 1 <= int(answer) <= len(keys):
        key = keys[int(answer) - 1]
        chosen = catalog.preset_norms(key)
        print(f"\nStarting from '{catalog.PRESETS[key][0]}':\n")
        for norm in chosen:
            print(f"   - {norm.statement}")
        return chosen
    return None


def choose_norms(groups: dict[str, list[Norm]] | None = None) -> list[Norm]:
    """Walk the catalog with the user and return what they picked."""
    groups = groups if groups is not None else catalog.GROUPS
    chosen: list[Norm] = []

    print("\nSentinel can watch for these common rules. Pick the ones that apply.")
    print("Answer 'a' to take a whole group, or just press Enter to skip a rule.\n")

    for title, norms in groups.items():
        print(f"-- {title}")
        take_all = False
        for norm in norms:
            if take_all:
                chosen.append(norm)
                continue

            kind = "regex" if norm.is_deterministic else "the model judges this"
            answer = _ask(f"   {norm.statement}\n     ({kind})  [y/N/a] ").lower()
            if answer.startswith("a"):
                take_all = True
                chosen.append(norm)
            elif answer.startswith("y"):
                chosen.append(norm)
        print()

    return chosen


def ask_for_custom_norms() -> list[Norm]:
    """The free-text step. These are usually the rules that actually matter."""
    print("Now the rules only your team knows.")
    print("Examples: 'no hardcoded model names anywhere', 'never change the")
    print("payment flow without asking', 'do not restyle the dashboard'.")
    print("One per line. Press Enter on an empty line when you are done.\n")

    custom: list[Norm] = []
    while True:
        statement = _ask(f"   rule {len(custom) + 1}: ")
        if not statement:
            break
        custom.append(
            Norm(
                id=_make_id(statement, {norm.id for norm in custom}),
                statement=_as_sentence(statement),
            )
        )

    return custom


def ask_for_severity() -> Verdict:
    """One question that sets how hard every chosen rule bites."""
    print("\nWhen the agent breaks one of these rules, what should Sentinel do?")
    print("  1. Flag it for review  - the verdict becomes REVIEW (recommended)")
    print("  2. Call STOP           - the verdict becomes STOP\n")
    print("Rules judged by the model can be wrong sometimes, so option 1 is the")
    print("safer default. You can change any single rule's severity in the file")
    print("afterwards.\n")

    answer = _ask("Choose [1/2]: ")
    return Verdict.BLOCKED if answer.startswith("2") else Verdict.FLAGGED


def to_config(norms: list[Norm], severity: Verdict) -> dict:
    """The `norms` section these choices produce."""
    rules = []
    for norm in norms:
        rule: dict = {"id": norm.id, "statement": norm.statement}
        # A norm the catalog already marks BLOCKED stays BLOCKED even when the
        # user picked the softer default: they opted into a rule whose whole
        # point is that it is not negotiable.
        rule["severity"] = (
            Verdict.BLOCKED.value if Verdict.BLOCKED in (norm.severity, severity) else Verdict.FLAGGED.value
        )
        if norm.patterns:
            rule["patterns"] = list(norm.patterns)
        if norm.applies_to:
            rule["applies_to"] = list(norm.applies_to)
        if norm.except_paths:
            rule["except_paths"] = list(norm.except_paths)
        rules.append(rule)

    return {
        "_comment": (
            "This project's rules for the coding agent. A rule with 'patterns' is checked by regex; "
            "a rule with only a 'statement' is judged by the model. Severity is set here by a human, "
            "never by the model. Edit freely - this is data, not code."
        ),
        "rules": rules,
    }


def merge_into(existing: dict, norms_section: dict) -> dict:
    """The existing config with its `norms` section replaced.

    Everything else in the file is left exactly as it was. Sentinel is not
    going to quietly rewrite a project's path rules because someone ran the
    norms wizard.
    """
    merged = dict(existing)
    merged["norms"] = norms_section
    return merged


def _make_id(statement: str, taken: set[str]) -> str:
    words = [word.strip("`'\".,()").lower() for word in statement.split() if word.strip("`'\".,()").isalnum()]

    # Built up whole words rather than cut at a character count, so a long rule
    # becomes `do-not-restyle-the-dashboard` and not `do-not-restyle-the-dashb`.
    slug = ""
    for word in words:
        if slug and len(slug) + len(word) + 1 > 40:
            break
        slug = f"{slug}-{word}" if slug else word

    candidate, suffix = slug or "custom-rule", 2
    while candidate in taken:
        candidate, suffix = f"{slug}-{suffix}", suffix + 1
    return candidate


def _as_sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


# --- the non-interactive core ---------------------------------------------
#
# The dashboard sets rules up too, from checkboxes instead of questions. Both
# front ends compose their choices here and write through `write_norms`, so a
# project configured from the GUI and one configured from the terminal are
# byte-identical for the same answers. Two writers would drift within a week,
# and a test pins that they do not (instructions.md #2).


def build_norms(selected_ids: list[str], custom_statements: list[str]) -> list[Norm]:
    """Catalog picks plus free-text rules, as one list of norms.

    Unknown ids are skipped rather than raised on: the catalog can lose a norm
    between releases, and a stale id in a saved form should cost that one rule,
    not the whole configuration.
    """
    chosen = [norm for norm in (catalog.by_id(norm_id) for norm_id in selected_ids) if norm is not None]

    taken = {norm.id for norm in chosen}
    for statement in custom_statements:
        text = statement.strip()
        if not text:
            continue
        norm_id = _make_id(text, taken)
        taken.add(norm_id)
        chosen.append(Norm(id=norm_id, statement=_as_sentence(text)))

    return chosen


def config_for(root: Path, norms: list[Norm], severity: Verdict) -> dict:
    """The whole config file this project would have, with these rules in it.

    Reads what is already there so every other section survives - Sentinel is
    not going to quietly rewrite a project's path rules because someone changed
    a norm.
    """
    target = root / CONFIG_FILENAME
    existing = read_config(target) if target.is_file() else {}
    return merge_into(existing, to_config(norms, severity))


def write_norms(root: Path, norms: list[Norm], severity: Verdict) -> Path:
    """Write these rules into the project's own config. Returns the path."""
    target = root / CONFIG_FILENAME
    merged = config_for(root, norms, severity)
    try:
        target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not write {target}: {exc}") from exc
    return target


def run(root: Path) -> int:
    target = root / CONFIG_FILENAME

    print(f"\nSetting up Sentinel for: {root}")
    print(f"Rules will be written to: {target}\n")

    existing: dict = {}
    if target.is_file():
        try:
            existing = read_config(target)
        except ConfigError as exc:
            print(f"There is already a config there and it could not be read: {exc}", file=sys.stderr)
            return 1
        already = len(existing.get("norms", {}).get("rules", []))
        print(f"This project already has a config with {already} rule(s).")
        print("Its other settings will be left alone; only the rules are replaced.\n")

    try:
        chosen = choose_preset()
        if chosen is None:
            chosen = choose_norms()
        chosen = chosen + ask_for_custom_norms()
        if not chosen:
            print("\nNo rules chosen, so there is nothing to write.")
            return 0

        severity = ask_for_severity()
        merged = merge_into(existing, to_config(chosen, severity))

        print(f"\nThis is what will be written to {target}:\n")
        print(json.dumps(merged["norms"], indent=2))

        if not _ask_yes_no("\nWrite this?", default=True):
            print("Nothing written.")
            return 0
    except Cancelled:
        print("\nCancelled. Nothing written.")
        return 1

    try:
        write_norms(root, chosen, severity)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"\nWritten. {len(chosen)} rule(s) now apply to this project.\n")
    print("Start watching, then let your coding agent work:")
    print(f"  python -m sentinel.action_monitor.watcher {root}\n")
    print("Afterwards, ask what it did and whether it followed the rules:")
    print(f"  python -m sentinel.explainer.explain {root}")
    print(f"  python -m sentinel.orchestrator.session_review {root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up Sentinel's rules for a project, by asking what they are.")
    parser.add_argument("path", nargs="?", default=".", help="the project to set up (default: current)")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1
    return run(root)


if __name__ == "__main__":
    raise SystemExit(main())
