"""Project norms - the rules this particular codebase holds its agent to.

Every project has rules no generic linter knows: "no hardcoded model ids",
"don't make frontend design calls without asking", "never widen a CORS policy".
This is where they live, and what checks them.

Two kinds, deliberately:

  * Norms with `patterns` are checked by regex. Deterministic, free, offline.
  * Norms with only a `statement` are judged by the model, because no regex can
    catch "don't redesign the UI without asking".

Both report the same `NormFinding`, tagged with which found it, and the
severity always comes from the human-authored config - never from the model.
"""
