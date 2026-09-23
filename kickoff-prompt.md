I'm starting a new project called Sentinel. Before writing any code, please do
the following, in order:

1. Read CLAUDE.md, instructions.md, and tasks.md in this repo — I've placed
   all three in the project root. These define the project scope, the coding
   principles you must follow, and the current task breakdown. Treat CLAUDE.md
   as the source of truth for what's in scope and out of scope — do not build
   anything listed under "Explicitly Out of Scope" even if a later request of
   mine seems to drift that way. If I ask for something that conflicts with
   CLAUDE.md's scope or instructions.md's principles, point it out to me
   instead of just proceeding.

2. Confirm you understand the project in your own words back to me before
   starting Phase 0 — specifically: what Sentinel does, what the four in-scope
   components are, and what architecture we're targeting (local dev LLM →
   AWS Bedrock → AgentCore hosting, with a CLI watcher + WhatsApp/Telegram +
   optional dashboard as interfaces).

3. Work through tasks.md top to bottom, one phase at a time. Update the
   checkboxes in tasks.md as you complete items — don't let it drift out of
   sync with actual progress. Do not start a new phase until the current
   phase's core tasks are done and independently testable.

4. Follow instructions.md strictly on every file you write: YAGNI, DRY without
   over-abstraction, no speculative features, no new dependencies without
   justification, every tool independently testable, graceful error handling,
   human-in-the-loop for anything with a real side effect.

5. We are a team of 3 students, first time using the Strands Agents SDK and
   AWS, with 25 days total to build this for the AWS "Agents for Humans"
   hackathon. Keep this constraint in mind for every implementation choice —
   favor the simpler, more defensible approach over the more impressive-
   sounding one. If you're about to reach for something complex (a graph
   database, a custom ML model, a new framework), stop and check whether a
   simpler heuristic solves the actual task first — this is called out
   explicitly in instructions.md #4 and CLAUDE.md's scope decisions.

6. Start with Phase 0 (Setup) once you've confirmed your understanding of the
   project. Ask me before making any decision that isn't already specified in
   CLAUDE.md, instructions.md, or tasks.md — for example, exact library
   choices, exact config file formats, or anything with more than one
   reasonable approach.

Do not start writing implementation code until you've done steps 1 and 2 above.
