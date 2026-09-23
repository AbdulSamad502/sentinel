# Coding Instructions — Read Before Every Task

These rules apply to all code written for Sentinel. They are not suggestions.
If a task seems to require breaking one of these, stop and flag it rather than
proceeding.

## 1. YAGNI (You Aren't Gonna Need It)

- Build only what the current task in `tasks.md` requires. Do not add
  configurability, abstraction layers, or "future-proofing" for features listed
  in CLAUDE.md's "Explicitly Out of Scope" section.
- No speculative parameters, flags, or extension points for hypothetical future
  use cases. If a need becomes real, add it then.
- Before adding any new file, class, or abstraction, ask: does a current,
  real task need this today? If not, don't write it.

## 2. DRY (Don't Repeat Yourself) — but not at the cost of clarity

- Shared logic (e.g. GitHub API calls, verdict formatting, permission checks)
  goes in one place, imported where needed. Do not copy-paste logic across
  sub-agents.
- Do not over-abstract to avoid three lines of duplication. A small amount of
  repetition is better than a confusing shared abstraction used in only two places.

## 3. Simplicity Over Cleverness

- Prefer the boring, obvious solution. This is a 25-day hackathon build reviewed
  by judges who will read the code — clarity scores higher than clever tricks.
- No premature optimization. No custom caching layers, no custom graph databases,
  no custom ML — unless a task explicitly calls for it.
- If a heuristic (e.g. file-churn count, keyword match on sensitive paths) does
  the job, use it. Do not reach for a more complex system "to be thorough."

## 4. Tool and Dependency Discipline

- Do not add a new dependency without checking if the standard library or an
  already-installed package can do it.
- No knowledge graphs, no vector databases, no custom ML models for this build.
  If a task seems to need one, stop and flag it — the answer is almost always
  a simpler heuristic (see CLAUDE.md's scope decisions on this exact question).
- GitHub API calls should go through one small, shared client module — not
  reimplemented per sub-agent.

## 5. Every Sub-Agent Tool Must Be Independently Testable

- Each Strands tool function (fetch, analyze, flag, etc.) should be callable
  and testable on its own, with mock/sample input, without needing the full
  orchestrator running.
- Write a small test or example call for each tool as you build it, not after.

## 6. Human-in-the-Loop for Anything Consequential

- Any action with a real-world side effect (sending a message, blocking a
  merge, taking an automated action beyond flagging/reporting) requires an
  explicit approval step. Sentinel's job in this build is to **flag and verdict**,
  not to autonomously act on the codebase.
- Never silently expand Sentinel's permissions or actions beyond "observe,
  analyze, report" without this being an explicit, discussed task.

## 7. Error Handling

- Every external call (GitHub API, LLM call, Bedrock call) must handle failure
  gracefully — no unhandled exceptions that crash the orchestrator mid-demo.
  A failed sub-agent check should degrade to "unable to verify X" in the
  verdict, not crash the whole flow.

## 8. Commit and File Hygiene

- Small, focused commits with clear messages.
- No dead code, no commented-out blocks left in. Delete or don't write it.
- Every file should have a clear, single responsibility matching one component
  from CLAUDE.md (Action Monitor, Code Risk Analyzer, Orchestrator, interfaces).

## 9. When Unsure

- If a task is ambiguous or seems to conflict with these principles or with
  CLAUDE.md's scope, stop and ask rather than guessing and over-building.
