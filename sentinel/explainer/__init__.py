"""Change Explainer - turning a recorded session into "here is what it did".

`diff.py` reconstructs the change from the session log and describes it without
any model at all. `explain.py` adds the plain-English narration on top.

The split matters: the deterministic summary is always available, so Sentinel
can still answer "what changed?" with no model running (instructions.md #7).
"""
