# Tasks — Sentinel Build

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done

Work top to bottom. Do not start a later phase before the current one's core
tasks are done and testable. Update status as you go.

---

## Phase 0 — Setup

- [x] Initialize repo, add Apache 2.0 (or MIT) LICENSE, base README
      (Apache 2.0. Project renamed Warden → Sentinel across all docs.)
- [x] Set up Python project structure (one folder per component, per CLAUDE.md)
- [x] Install Strands Agents SDK, confirm a minimal "hello world" agent runs locally
      (`tests/hello_agent.py` — verified tool-calling works, not just text)
- [x] Set up local LLM for dev (Ollama or similar) — confirm Strands can call it
      (`sentinel/llm.py` — model picked at runtime from what each laptop has
      installed, since the team runs different hardware)
- [~] Create AWS Builder ID + AWS account — with the team, see `docs/aws-setup.md`
- [~] Request hackathon's $50 AWS credit via Resources tab — see `docs/aws-setup.md`
- [ ] Confirm Bedrock access works with a trivial test call (do this early — auth/
      access setup can have delays, don't discover this in week 3)
      (Blocked on the account above. Test command is step 7 of `docs/aws-setup.md`,
      and everything that follows it is step 1 of `docs/deploy-agentcore.md`.)

## Phase 1 — Action Monitor

- [x] Define the "dangerous actions" list (deletions, dependency changes, force
      pushes, changes to config/secrets files, etc.) — keep as simple config,
      not hardcoded logic
      (`sentinel.config.json` — all data. No rule is hardcoded in Python.)
- [x] Define folder/path permission model: allow-list and deny-list of paths,
      loaded from a simple config file (`sentinel.config.json`)
      (deny beats allow; an empty allow list means "anywhere not denied")
- [x] Build the watcher: hooks into file-system events or git diff/status to
      detect actions taken by the coding agent
      (`sentinel/action_monitor/watcher.py`, filesystem events via watchdog.
      Run: `python -m sentinel.action_monitor.watcher <path>`)
- [x] Build the check: given an action + the permission config, return
      ALLOWED / FLAGGED / BLOCKED with a reason
      (`rules.check()` — pure function. Worst finding wins, all reasons kept.)
- [x] Test independently with a set of mock actions (see instructions.md #5)
      (52 tests, stdlib unittest: `python -m unittest discover -s tests -t .`)

**That known gap is now closed.** The rules understood dangerous *git commands*
from the start but nothing fed real ones in, because a filesystem event cannot
see a `git push --force`. `action_monitor/githooks.py` installs `pre-push`,
`pre-commit` and `pre-rebase` hooks that report the command git was actually
given, flags included, read from the hook's parent process.

Verified against a real repo: `git push --force origin master` is captured,
judged BLOCKED, printed inline — **and the push still succeeds.** Every hook
ends `exit 0` unconditionally, because a non-zero exit would abort the push and
Sentinel does not act (instructions.md #6).

Still not covered, and stated rather than hidden: `reset --hard`, `clean -fd`
and `branch -D` have no git hook at all. They stay in the config as policy.

## Phase 2 — Code Risk Analyzer

- [x] GitHub API client (shared module, used by this and other components)
      (`sentinel/shared/github.py` — stdlib urllib, read-only by design, a test
      asserts no write methods exist)
- [x] Fetch changed files for a given commit/PR
- [x] Sensitive-path keyword check (payments/, auth/, checkout/, config/, etc.
      — keep as a simple configurable list)
- [x] File-churn signal: how often has this file changed recently (simple count,
      not a graph)
- [x] Missing-test heuristic: does this change touch source files with no
      corresponding test file change
- [x] Combine into a per-file and per-change risk score with reasons
      (`code_risk/analyzer.py` — LOW / MEDIUM / HIGH, pure functions)
- [x] Test independently against a real public repo's recent PRs
      (`python tests/live_pr_check.py django/django` — verified against
      psf/requests and django/django)

**Two false positives found by running it live, now fixed and covered by tests:**
a stylesheet at `admin/css/widgets.css` was scored HIGH as "permissions code"
purely because `admin` was in its path (assets and docs are now exempt); and
churn read "30 commits" when 30 was just the API page size, so it now says
"at least 30".

**Open judgment call for the team:** a *test* file in a sensitive area (e.g.
`tests/admin_widgets/tests.py`) currently scores HIGH. Arguable either way —
weakening a permissions test is a real risk, but it reads oddly. Left as-is;
change `sensitive_areas()` in `code_risk/analyzer.py` if the team disagrees.

## Phase 3 — Orchestrator (Risk Correlation + Reliability Verdict)

- [x] Define the verdict output format: SAFE / REVIEW / CONDITIONAL / STOP +
      plain-English reasons (match the example tone from the project brief)
      (`orchestrator/verdict.py` — `Assessment.report()`)
- [x] Build the orchestrator agent that calls Action Monitor + Code Risk
      Analyzer (and CI/test status if time allows) and synthesizes one verdict
      (`orchestrator/review.py` gathers; `verdict.synthesize()` decides.
      CI status is included, via GitHub check-runs.)
- [x] Handle partial failure gracefully (see instructions.md #7) — e.g. if CI
      status is unavailable, verdict should say so, not crash
      (each signal gathered in isolation; failures land in `unchecked` and cap
      the verdict at REVIEW. Tested with a stub client that fails on demand.)
- [x] Test end-to-end against a real repo with a deliberately risky PR planted
      for demo purposes
      (`tests/demo_scenario.py` — scripted agent session, real PR optional.
      Also verified live: `python -m sentinel.orchestrator.review django/django 21793`)

**Key design decision — the LLM does not decide the verdict.** `synthesize()` is
deterministic, so identical signals always give an identical verdict. The Strands
agent in `orchestrator/agent.py` only restates a verdict already decided, and
falls back to the plain report if the model is unavailable. A supervisor whose
answer changes between runs on the same input is not a supervisor. Do not move
the decision into the model in Phase 4 or 5.

## Phase 3.5 — Change Explanation + Project Norms

Added after Phase 3, before Phase 4, on an explicit decision (see CLAUDE.md's
components 5 and 6). **What this displaced:** nothing in Phase 4 or 5 was cut.
The optional web dashboard in Phase 4 is now unlikely to fit — treat it as
dropped unless everything else lands early.

- [x] Session recording: snapshot the tree at watcher start, record every
      action, so a second process can reconstruct the change
      (`sentinel/session.py` -> `<repo>/.sentinel/session/`. The snapshot has to
      be taken at start: watchdog fires *after* a write lands.)
- [x] Change Explainer: reconstruct the diff and describe it
      (`explainer/diff.py` is pure and needs no model; `explainer/explain.py`
      adds narration and falls back to the deterministic summary.
      `--plain` doesn't even import Strands.)
- [x] Project norms: plain-English rules per project, checked on every change
      (`norms/` — regex norms are deterministic, statement-only norms are judged
      by the model, severity is always human-authored config.)
- [x] Interactive setup: ask what the project's rules are when Sentinel is
      pointed at it (`norms/wizard.py`, offers the built-in catalog plus
      free-text rules, merges into the repo's own `sentinel.config.json`)
- [x] Wire norms into the verdict (`synthesize(..., norm_findings=)`) and add
      the local `review_session()` path plus its CLI
- [x] Config can live in the watched repo (`resolve_config_path`), so each
      project carries its own rules
- [x] Tests: 95 new, all offline (`test_session.py`, `test_explainer.py`,
      `test_norms.py`, plus norm rungs added to `test_orchestrator.py`)

**Dropped deliberately: "Sentinel talks to the coding agent and asks it to fix
things."** It would make Sentinel *act*, which instructions.md #6 and CLAUDE.md's
"Sentinel never acts" both forbid, and it needs a channel back into the coding
agent that does not exist. Replaced with `--fix-prompt`, which composes the
correction message and **prints it** for a human to send. If this comes back as
a request, it is a scope decision for the whole team, not a code change.

**The catalog's regexes were audited against botocore, sqlalchemy, numpy,
flask, requests and urllib3 before shipping**, which caught three false-positive
classes including one that would have produced a false STOP. Every one is now a
"must not match" case in `tests/test_norms.py`. Do the same for any new pattern:
`python -m sentinel.norms.checker --audit <repo> --builtin`.

## Phase 4 — Natural-Language Control (interfaces)

- [x] Simple query interface into the orchestrator: "What went wrong?",
      "Why did you stop it?", "Can we ship?" — answered from current state
      (`interfaces/query.py`. Seven intents, routed by deterministic keyword
      matching — free, and identical on every run. Only an unrecognised question
      reaches a model, and it is handed the finished `Assessment.report()`
      rather than raw signals, so it can restate but never decide.
      Try it: `python -m sentinel.interfaces.query <repo> "can we ship?"`)
- [x] WhatsApp or Telegram bot wired to this query interface (pick one — do not
      build both unless Phase 1-3 finish early)
      (**Telegram**, chosen because WhatsApp needs Meta business verification and
      template approval — days of latency outside our control, on the demo path.
      `interfaces/telegram.py`, stdlib urllib only, no new dependency.)
- [ ] (Optional, if time allows) Minimal web dashboard showing latest verdict +
      flagged actions — for Design score
      (**Deliberately deferred.** Phase 5 is a hard submission requirement and
      has not started; Bedrock access is still blocked back in Phase 0. Revisit
      only if Phase 5 and 6 land early.)

**Two rules the bot is built around, both load-bearing:**

*The allow-list is a security boundary.* Answers quote real source lines out of
a private repo, and anyone can find a Telegram bot by name. An empty
`SENTINEL_TELEGRAM_ALLOWED_CHATS` means *answer nobody*. A stranger gets silence
rather than a refusal, because a refusal confirms the bot is worth probing.

*It replies; it never initiates.* No push alerts (Phase 6's demo beat is a
**query** moment), and it never messages the coding agent — `/fix` returns the
correction text to the human, who sends it or doesn't (instructions.md #6).

**The LLM paths are now verified end to end** against Ollama, which closes the
last unknown before Phase 5:

- `tests/hello_agent.py` — Strands reaches Ollama, tool-calling works.
- `explainer.explain_session` — narrated a real 9-file session accurately, ~42s.
- `agent.explain` — restated a STOP verdict without altering it.
- `norms.check_statements` — **the path that had never run.** It correctly
  flagged `stay-on-task` on the two files in that session that were fixes
  outside the phase's scope. Strict JSON parsed first time on `gemma4:12b-mlx`,
  and on `deepseek-r1:14b` too.

No code changes were needed for any of it. Timings are in CLAUDE.md — plan the
demo around them rather than discovering them while recording, and set
`SENTINEL_MODEL` so nothing stops to ask which model to use.

## Phase 4.5 — Prevention, not just detection

Added on an explicit decision after Phase 4, ahead of deployment. Both items
extend components 1 and 4 rather than adding a pillar.

- [x] Real git command capture (`action_monitor/githooks.py`) — see the Phase 1
      note above, which this closes.
- [x] MCP server so the **coding agent itself** can consult Sentinel
      (`interfaces/mcp_server.py`). Three read-only tools:
      `sentinel_project_rules` (call it *before* writing — this is the whole
      point), `sentinel_what_changed`, `sentinel_check_my_work`.
      No new dependency: the `mcp` SDK ships with `strands-agents`.
      Verified with a real stdio handshake, not just unit tests.
- [x] Consultation log, so self-correction does not cost the human oversight.
      Every tool call is recorded and reported next to the verdict: *"the coding
      agent consulted Sentinel 3 time(s): check_my_work x1, project_rules x1."*

**The trap this was designed around.** An agent that can query its supervisor
and retry until it goes green is optimising to pass Sentinel, which is not the
same as writing good code — and the human stops seeing what went wrong, because
it was all quietly fixed. Hence the log, and hence `check_my_work` telling the
agent plainly that the developer sees the same report. If this ever becomes a
tool that *fixes* things, the project has changed into something else.

Register it with a coding agent:

```json
{"mcpServers": {"sentinel": {
    "command": "python",
    "args": ["-m", "sentinel.interfaces.mcp_server", "/path/to/repo"]}}}
```

## Phase 4.6 — Product polish (features 1-6)

Chosen from a ranked list; 7 and 8 (multi-repo supervision, violation trends)
were deliberately declined.

- [x] **Recovery hints.** A deleted file is already in the baseline snapshot, so
      the verdict now offers the exact `cp` to restore it. A test actually runs
      the offered command from an unrelated directory and checks the file comes
      back — a recovery hint that does not work is worse than none.
- [x] **Code Risk on local sessions.** `review_session()` scored actions and
      norms but never risk, even though local sessions carry real diffs. Now
      wired, and verified: editing `src/payments/charge.py` moves the local
      verdict to CONDITIONAL, which it could not detect before.
- [x] **Local churn** (`code_risk/churn.py`), so the fourth risk signal works
      off the local git history instead of being a permanent caveat.
- [x] **Session timeline** — `--timeline` and `--since 2h`, merging file
      actions, git commands and agent consultations into one ordered stream.
- [x] **`sentinel doctor`** — config, rules, session, hooks, model, MCP. Nothing
      that still works is called a failure, and every non-passing check carries
      a remedy (a test enforces that).
- [x] **`--markdown` and `--exit-code`** — a pasteable PR comment, and a
      non-zero exit on STOP so a pipeline *configured by a human* can gate on it.
- [x] **Norm presets** — web, frontend, ai, payments, library. The wizard now
      opens with one question instead of thirteen.

369 tests, still fully offline, still under a second.

## Phase 4.7 — The dashboard

- [x] `interfaces/export.py` — a session as one JSON document. A serializer over
      `review_session()`, never a second verdict.
- [x] `interfaces/dashboard.py` — stdlib `http.server`, rebuilds the document
      per request, **bound to 127.0.0.1 and verified refused from the LAN**.
- [x] `dashboard/` — React + Vite + TypeScript. One app, two data sources,
      switched by `VITE_SESSION_URL` alone: a recorded session when hosted, the
      live server when local.
- [x] A file navigator with the feel of Finder/Explorer — one implementation for
      every platform, using what both share: chevrons, folders before files,
      indent guides, arrow-key navigation. Folders inherit the worst severity
      inside them, and a folder holding one folder folds into a single row.
- [x] 14 tree tests via `node --test`, which strips types itself — no test
      dependency added.

Verified in a real browser at desktop and mobile widths, in light and dark, in
both data modes. Two bugs came out of that: `scrollIntoView` on mount was
dragging the reader straight past the verdict, and the indent guide referenced a
CSS variable that was never set.

**Still to do for hosting:** Cloudflare Pages build command
`npm install && VITE_SESSION_URL=./sample-session.json npm run build`, output
`dist`. No backend — the real one ships inside the Tauri app.

## Phase 4.8 — The dashboard becomes the product surface

The dashboard was a viewer: you ran the wizard in one terminal and the watcher
in another, and the page showed the result. A judge opening it saw an empty
screen unless they first learned three commands. That is backwards, so the page
now drives Sentinel instead of trailing it.

- [x] **Agent registry** (`sentinel/agents.py`) — a per-user list of supervised
      repos in `~/.sentinel/agents.json`. The first state Sentinel has had that
      is scoped to a person rather than a place.
- [x] **Process supervisor** (`sentinel/supervisor.py`) — starts and stops
      watchers by spawning the existing CLI entry point, so the GUI and the
      terminal cannot drift. Also the job runner for the slow features, which
      cannot be synchronous HTTP calls at ~1m46s each.
- [x] **The guard** (`interfaces/guard.py`) — token, origin, content type, path
      confinement. Localhost stopped being a boundary the moment POST routes
      could start processes.
- [x] **Folder picker, norms editor, sidebar, settings, CLI panel** — the whole
      first-run flow with no terminal: pick a folder, tick rules, press start.
      Rules are written into that repo's own config and never asked for again.
- [x] **"Visualize": a rotatable 3D structure** (`SpaceView` + `lib/space.ts`).
      The verdict at the centre, a hub per folder, files on their hub, edges
      that are the real tree. Drag to rotate, wheel to zoom, turns slowly on its
      own unless `prefers-reduced-motion`. Hand-rolled projection rather than a
      3D dependency: one rotation, one perspective divide and a depth sort, all
      pure and testable. Shares one selection with the file tree.
- [x] **A collapsible sidebar** — collapses to a 46px rail that keeps the agent
      status dots and the way back visible. A panel with no visible way back is
      a panel people lose.
- [x] **Asking a question scrolls to the answer.** A model-backed question takes
      about a minute; without the scroll the panel opened above whatever you
      were reading and the page looked like it had ignored you.
- [x] 51 new Python tests and 24 new layout tests. 461 Python + 39 dashboard,
      offline, under a second.

**Verified end to end in a real browser**: added a folder, applied the payments
preset plus two typed rules, started the agent from the page, edited five files,
and watched the verdict, the findings and the orbit fill in. Both custom rules
were caught by the model. The recorded read-only build still renders with zero
control affordances, which is what the hosted demo link depends on.

**Five real bugs came out of that session**, all listed in CLAUDE.md's "things
that bit us" — the worst being that the polling live view was spending a
one-minute model call every four seconds.

## Phase 4.9 — Telegram, from the page

- [x] **Set the bot up with no terminal at all** — five steps in the dashboard:
      create it in @BotFather, paste the token (validated by `getMe` before it
      is stored), message the bot and press Find me, approve the chat, pick the
      repo, start. The token lives in `~/.sentinel/telegram.json` at mode 0600,
      never in `sentinel.config.json`, and the page only ever sees its last four
      characters.
- [x] **Drive Sentinel from your phone** — `/agents`, `/use 2`, `/watch`,
      `/unwatch`. A session started away from the desk is still supervised.
      Matched as exact commands, before the question path, so no phrasing can
      spawn a process.
- [x] **Watchers and the bot outlive the dashboard.** `stop_all()` is gone;
      closing the page says what is still running instead.
- [x] The Telegram commands are in the terminal panel too, with the token shown
      as a placeholder and never the real value.
- [x] 21 new tests. 482 total, offline, under a second.

**The one human step left** is creating the bot in @BotFather. Everything after
that is buttons.

## Phase 5 — Deployment

**The code is done. What is left is account work**, written up step by step in
`docs/deploy-agentcore.md`. Nothing below needs another line of Python.

- [x] Swap local LLM for AWS Bedrock as the model provider
      (`sentinel/llm.py`. Three providers behind one function - `ollama`,
      `bedrock`, `agentcore` - chosen by `SENTINEL_PROVIDER`, with settings
      read from the environment first and then the `model` section of
      Sentinel's own config. `run_agent()` is now the only place in the repo
      that builds a Strands `Agent`; the four call sites lost their duplicated
      setup and their duplicated error handling with it.)
- [x] Deploy orchestrator + sub-agents on AWS Bedrock AgentCore
      (**code and container done, not yet deployed** - Bedrock model access is
      still pending. `interfaces/agentcore_app.py` implements AgentCore
      Runtime's contract on stdlib `http.server`: `POST /invocations`,
      `GET /ping`, port 8080. `deploy/Dockerfile` builds the arm64 image.)
- [x] Confirm the WhatsApp/Telegram interface and/or dashboard talk to the
      hosted AgentCore backend, not the local dev version
      (Nothing to wire: every interface goes through `query.answer()` and every
      model call through `run_agent()`, so setting `SENTINEL_PROVIDER` moves
      all of them at once. Verified end to end against a locally-hosted
      runtime - client -> HTTP -> service -> model - with only AWS itself
      stubbed out.)
- [ ] Request Bedrock model access, then run `docs/deploy-agentcore.md`
      (**the blocker, and the only one.** Approval latency is not ours to
      control.)
- [ ] (Optional) Expose a live demo link

**What is hosted, and what is deliberately not.** The runtime takes a system
prompt and a prompt and returns text. It never sees a session, never runs
`synthesize()`, and a test walks its AST to prove it has no import or call that
could reach a verdict. The verdict is computed on the machine that has the
code, from data that never left it — which is how hosting the model avoids
handing the model the decision.

**The regression test that makes the swap safe.** The same session runs through
all three providers with each transport stubbed to the same answer, and the
report must come back byte-identical. If a provider could move a verdict, a
model has got into the decision and the project's central promise is broken.

**The privacy trade-off, stated rather than buried.** On `agentcore`, diffs
leave the machine. `SENTINEL_PROVIDER=ollama` is one variable and nothing does.
That is also the demo's rollback if the endpoint is down on the day.

410 tests, still fully offline, still under a second.

## Phase 6 — Submission Prep

- [ ] Architecture diagram (matches CLAUDE.md's architecture section)
- [ ] README: what it does, who it's for, how it works, setup instructions
- [ ] Confirm LICENSE is visible in repo's About section
- [ ] Record demo video (≤5 min): problem → who it's for → live walkthrough
      (plant a clear risky-change scenario) → verdict → WhatsApp query moment
- [ ] Write submission text description
- [ ] (Bonus) Publish build-journey post on builder.aws.com with "Agents for
      Humans" in the title
- [ ] Double-check every required submission item against the hackathon rules
      before final submit

---

## Explicitly Not a Task (do not add without discussion)

Anything from CLAUDE.md's "Explicitly Out of Scope" list. If a task like this
appears here later, it should come with an explicit note on what got cut to
make room for it.
