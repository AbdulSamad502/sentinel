"""Phase 0 smoke test: proves Strands, Ollama, and tool-calling all work.

Run it directly:

    python tests/hello_agent.py

It checks three things at once — the SDK is installed, the local model is
reachable through it, and the model can actually call a tool. Tool-calling is
the part everything later depends on, so it is worth confirming before any real
component is built.

This is also the shape every later tool's standalone test should take
(instructions.md #5): callable on its own, no orchestrator needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strands import Agent, tool

from sentinel.llm import ModelSetupError, get_model


@tool
def count_files(directory: str) -> str:
    """Count the files in a directory.

    Args:
        directory: Path to the directory to count files in.
    """
    path = Path(directory)
    if not path.is_dir():
        return f"{directory} is not a directory."
    return f"{directory} contains {sum(1 for item in path.iterdir() if item.is_file())} files."


def main() -> int:
    try:
        model = get_model()
    except ModelSetupError as exc:
        print(f"\nCannot start: {exc}\n", file=sys.stderr)
        return 1

    print(f"\nUsing model: {model.get_config()['model_id']}\n")

    agent = Agent(
        model=model,
        tools=[count_files],
        system_prompt=(
            "You are a setup check for Sentinel. Answer in one short sentence. "
            "Use the tools you are given rather than guessing."
        ),
    )

    repo_root = Path(__file__).resolve().parent.parent
    agent(f"How many files are directly inside {repo_root}?")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
