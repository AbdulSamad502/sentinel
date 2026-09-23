# Sentinel — Project Context

## What Sentinel Is

Sentinel is a **supervisory agent for AI coding agents**. It does not write code itself.
It watches an AI coding agent (e.g. Claude Code, Cursor, Copilot Workspace) while that
agent works on a codebase, and it answers one question continuously: **is what this
agent is doing safe, correct, and shippable?**

Built for: AWS "Agents for Humans" Hackathon — Professional Agents track.
Stack: Strands Agents SDK, AWS Bedrock (LLM provider), AWS Bedrock AgentCore (hosting),
GitHub API, WhatsApp/Telegram (remote query interface).

## Why This Exists (do not lose this framing)

Developers increasingly delegate real work to AI coding agents. Those agents can:
- touch files they shouldn't
- delete or overwrite things silently
- claim "done" without the change actually working
- make security-sensitive changes without review
- go off-track from what was actually asked

Sentinel sits alongside the coding agent as an independent, skeptical observer and
gives a clear verdict: **SAFE / REVIEW / CONDITIONAL / STOP**, with reasons.

## In-Scope Components (build these, deeply)

1. **Action Monitor** — watches the coding agent's actions (file writes, deletes,
   dependency changes, git commands). Enforces folder/path permissions (allow-list /
   deny-list). Flags dangerous actions in real time, before or as they happen.
2. **Code Risk Analyzer** — reviews changed files for: security-sensitive paths,
   missing tests, unusually large diffs, high file-churn history.
3. **Risk Correlation + Reliability Verdict** — the orchestrator's synthesis step.
   Combines Action Monitor + Code Risk Analyzer + Project Norms + CI/test signal
   into one verdict (SAFE / REVIEW / CONDITIONAL / STOP) with plain-English reasons.
4. **Natural-Language Control** — query interface (WhatsApp/Telegram + simple web
   dashboard) for questions like "What went wrong?", "Why did you stop it?",
   "Can we ship?" — answered by the orchestrator from its current state.
5. **Change Explainer** — answers "what did it just do?". An agent rewrites twelve
   files in ninety seconds; reading that by hand is slow, and asking the coding
   agent to summarise its own work costs tokens and asks the suspect to write the
   report. Sentinel was watching independently, so it explains from what it saw.
6. **Project Norms** — the rules *this* project holds its agent to, in plain
   English ("no hardcoded model ids", "don't make frontend design calls without
   asking"). Declared once when you point Sentinel at a folder, then checked on
   every change. Regex-checkable norms are deterministic; the rest are judged by
   the model, but their severity is always human-authored.

5 and 6 are depth on 1–3 rather than new pillars: norms are another signal into
the same verdict, and the explainer reads the same session the Action Monitor
records. Scope discipline still applies — they earned their place because
"is this safe?" is only half of what a supervisor is asked, and "what did it
actually do?" is the other half.

## Explicitly Out of Scope for the Hackathon Build

Do NOT build these. If asked to, push back and point here instead. They are
roadmap items to mention in the pitch, not code to write:
- Context Guardian (detecting "context degradation" in another agent's session)
- Task Contract (auto-converting requests into formal requirements)
- Independent Verification (general-purpose "does this actually work" testing)
- Session Memory (persistent architecture/decision memory across sessions)
- Model selection / model-effort recommendation
- Context optimization for the coding agent
- Any native app packaging (.exe, .dmg, installers)

Scope discipline matters more than feature count. Four components built deeply
beats ten built shallowly.

## Architecture (locked)

```
Local dev: Strands agent + local LLM (fast iteration, no AWS cost)
        ↓ once stable
AWS Bedrock: swap in as LLM provider
        ↓
AWS Bedrock AgentCore: hosts orchestrator + sub-agents as a callable service
        ↓
Interfaces:
  - Lightweight local CLI/watcher (observes the actual coding agent's file/git
    actions, sends events to the hosted orchestrator)
  - WhatsApp/Telegram bot (remote natural-language queries)
  - Simple web dashboard (optional, shows verdicts + flagged actions — for Design score)
```

No native installers. No bundling the LLM into a desktop binary. The orchestrator
is a hosted service; clients (CLI watcher, bot, dashboard) talk to it.

## Team Constraints (keep decisions grounded in these)

- Team of 3, first time using Strands SDK and AWS
- 25 days total build time
- Must produce: text description, public repo (MIT or Apache 2.0 license, README,
  setup instructions), architecture diagram, demo video (≤5 min), AWS Builder ID,
  optionally a live demo link (via AgentCore)

## Coding Principles

See `instructions.md` for the strict coding rules (YAGNI, DRY, etc.) that apply to
every file in this repo. Read it before writing code, not after.

## Current Task Tracking

See `tasks.md` for the actual task breakdown and status. Update it as work
progresses — do not let it drift out of sync with real progress.

---

# Repo State — keep this section current

Everything below describes what actually exists. If you change how the project
is laid out, run, or configured, update this section in the same change. It is
the first thing a teammate's Claude Code session reads.

## Layout

```
sentinel/
  llm.py                  # the ONLY module that knows which LLM provider we use.
                          #   run_agent() is the ONLY place a Strands Agent is built.
  remote.py               # the client for the hosted runtime (SENTINEL_PROVIDER=agentcore)
  agents.py               # the per-user registry: which repos are supervised
  supervisor.py           # starts/stops watcher processes + the slow-job runner
  session.py              # records a watching session to <repo>/.sentinel/session/
  doctor.py               # "will Sentinel work here?" - one command, actionable
  action_monitor/
    actions.py            # Action, ActionType, Verdict, Decision — shared vocabulary
    config.py             # loads sentinel.config.json into MonitorConfig
    rules.py              # check(action, config) -> Decision. Pure, no I/O.
    githooks.py           # real git commands -> Actions. Hooks NEVER block.
    tree.py               # IGNORED_DIRECTORIES, is_noise, walk_files — shared by
                          #   the watcher and the session log
    watcher.py            # watchdog -> Actions -> check() -> printed verdicts
  code_risk/
    analyzer.py           # sensitive paths, churn, missing tests, diff size -> RiskLevel
    churn.py              # local git history. The ONLY module that shells out to git.
    config.py             # loads the code_risk section
  explainer/
    diff.py               # session -> FileChanges + summarise(). Pure, no model.
    explain.py            # the plain-English narration, + CLI
    config.py             # loads the explain section (token budgets)
  norms/
    norms.py              # Norm, NormFinding — reuse Verdict, no fourth vocabulary
    catalog.py            # the built-in norms the wizard offers
    config.py             # loads the norms section
    checker.py            # check_patterns (pure) + check_statements (model), + --audit
    wizard.py             # interactive setup: "what are this project's rules?"
  orchestrator/
    verdict.py            # SAFE/REVIEW/CONDITIONAL/STOP. Pure, deterministic, NO LLM.
    review.py             # gathers signals, isolates failures. PR + session paths.
    session_review.py     # the verdict on a local session. CLI.
    markdown.py           # the verdict as a pasteable PR comment
    agent.py              # Strands agent that only RESTATES a decided verdict
  interfaces/
    query.py              # question -> intent -> answer. THE Phase 5 swap seam.
    telegram.py           # the bot: stdlib urllib, allow-listed, reply-only
    mcp_server.py         # the CODING AGENT consults Sentinel. Read-only tools.
    export.py             # session -> one JSON document for the dashboard
    dashboard.py          # stdlib http.server, localhost only
  shared/
    config_file.py        # reads/validates sentinel.config.json for ALL loaders
    github.py             # the ONLY place we call the GitHub API
dashboard/                # React + Vite + TS. See dashboard/README.md.
  src/lib/tree.ts         # flat paths -> navigable tree. Tested with node --test.
  src/lib/space.ts        # the 3D layout + projection. Pure, tested with node --test.
  src/components/         # Sidebar, FolderPicker, NormsEditor, SpaceView, Panes
  public/sample-session.json  # a real recorded session, for the hosted build
tests/
  offline.py              # go_offline() - forces a test to run with no model
  hello_agent.py          # Phase 0 setup check
  live_pr_check.py        # needs network, deliberately NOT in the suite
  test_action_monitor.py  # stdlib unittest, no pytest
  test_watcher.py
  test_code_risk.py
  test_github.py
  test_orchestrator.py
  test_session.py
  test_explainer.py
  test_norms.py
  test_query.py
  test_telegram.py
  test_githooks.py
  test_mcp_server.py
  test_recovery_and_risk.py
  test_tooling.py
  test_aws.py             # providers, the hosted runtime, and the swap regression
  test_control.py         # registry, guard, supervisor, jobs, and the API routes
  demo_scenario.py        # the demo: a coding agent goes off the rails
sentinel.config.json      # the permission policy, the norms AND the model provider
deploy/Dockerfile         # the arm64 runtime image AgentCore runs
docs/aws-setup.md         # account/Bedrock steps (human-only work)
docs/deploy-agentcore.md  # Phase 5's account work, step by step
```

## Running things

```bash
python tests/hello_agent.py                            # setup check: Strands + Ollama + tool-calling
python -m unittest discover -s tests -t .              # all tests (461, ~0.9s)
python tests/live_pr_check.py django/django            # score a real PR (needs network)
python -m sentinel.orchestrator.review django/django 21793   # full verdict on a PR
python tests/demo_scenario.py                          # the demo, offline
python tests/demo_scenario.py --explain                # ... plus the LLM summary
python tests/demo_scenario.py --fix-prompt             # ... plus the correction message
```

The local loop, which is the one a developer actually lives in:

```bash
python -m sentinel.norms.wizard <repo>                 # ask what this project's rules are
python -m sentinel.action_monitor.watcher <repo>       # watch; snapshots the tree first
# ... let the coding agent work ...
python -m sentinel.explainer.explain <repo>            # what did it do?
python -m sentinel.explainer.explain <repo> --plain    # ... deterministically, zero tokens
python -m sentinel.explainer.explain <repo> --detail src/x.py   # one file, in depth
python -m sentinel.orchestrator.session_review <repo>  # can we ship it?
python -m sentinel.orchestrator.session_review <repo> --fix-prompt
python -m sentinel.orchestrator.session_review <repo> --markdown    # PR comment
python -m sentinel.orchestrator.session_review <repo> --exit-code   # gate CI on STOP
python -m sentinel.explainer.explain <repo> --timeline              # in order
python -m sentinel.explainer.explain <repo> --since 2h              # since you left
python -m sentinel.doctor <repo>                                    # is this set up right?
python -m sentinel.interfaces.dashboard                             # the dashboard (all repos)
python -m sentinel.interfaces.export <repo> -o session.json         # ... or just the data
```

Let the coding agent report its own git commands, and consult Sentinel as it works:

```bash
python -m sentinel.action_monitor.githooks --install <repo>   # push/commit/rebase
python -m sentinel.action_monitor.githooks --uninstall <repo>
python -m sentinel.interfaces.mcp_server <repo>               # launched by the agent
```

Or ask in words, from a terminal or from your phone:

```bash
python -m sentinel.interfaces.query <repo> "can we ship?" --intent
export SENTINEL_TELEGRAM_TOKEN=...            # from @BotFather
export SENTINEL_TELEGRAM_ALLOWED_CHATS=<your chat id>
python -m sentinel.interfaces.telegram <repo>
```

Before trusting any new norm pattern, prove it against a real repo:

```bash
python -m sentinel.norms.checker --audit <some-real-repo> --builtin
```

Which brain it runs on, one variable (see `docs/deploy-agentcore.md`):

```bash
SENTINEL_PROVIDER=ollama      # default. A model on this laptop, nothing leaves it.
SENTINEL_PROVIDER=bedrock     # AWS Bedrock, with this machine's AWS credentials.
SENTINEL_PROVIDER=agentcore   # Sentinel's hosted runtime. No Ollama, no AWS account.

python -m sentinel.interfaces.agentcore_app     # run the hosted service locally
docker build --platform linux/arm64 -f deploy/Dockerfile -t sentinel-runtime .
```

Set `GITHUB_TOKEN` to raise GitHub's limit from 60 to 5000 requests an hour.

First run asks which local Ollama model to use and remembers the choice per
laptop in `.sentinel/` (gitignored) — teammates have different models pulled.
Set `SENTINEL_MODEL=<id>` to skip the prompt; set `SENTINEL_OLLAMA_HOST` if
Ollama isn't on the default port.

## Conventions decided so far

- **Model access goes through `sentinel.llm.run_agent()`.** Never import a model
  provider, and never build a Strands `Agent`, anywhere else. Three providers
  now live behind that one function - `ollama`, `bedrock`, `agentcore` - and
  adding a fourth must stay a one-file change. `get_model()` is still there for
  the two providers that have a local model; `agentcore` deliberately has none
  and refuses that call rather than quietly answering with a different one.
- **The provider is read from Sentinel's own config, never a watched repo's.**
  Every other setting prefers `<watched-repo>/sentinel.config.json`; this one
  must not, or a repo we are supervising could point our model calls at
  something it controls. A test pins that `llm.py` never calls
  `resolve_config_path`.
- **The hosted runtime is a model, not the orchestrator.** `agentcore_app.py`
  takes a system prompt and a prompt and returns text. It never sees a session,
  never runs `synthesize()`, and a test walks its AST to prove it has no import
  or call that could reach a verdict. Hosting the model must not hand the model
  the decision - that is the same rule as always, enforced across a network.
- **Swapping the provider cannot move a verdict, and that is a test.**
  `tests/test_aws.py` runs the same session through all three providers with
  each transport stubbed to the same answer, and asserts the report is
  byte-identical. If that ever fails, a model has got into the decision.
- **Policy lives in `sentinel.config.json`, not in Python.** Making Sentinel
  stricter or looser should never require editing code. If you find yourself
  hardcoding a path or a rule, put it in the config instead.
- **Path patterns are globs on repo-relative, forward-slash paths**, matched
  with `fnmatchcase`. Case-sensitive on every OS deliberately — `fnmatch` folds
  case on Windows only, which would give teammates different verdicts from the
  same config. Note `*` crosses `/`, so `src/*` matches `src/deep/file.py`.
- **`check()` is pure.** No file reads, network, or clock inside it, so it stays
  testable against mock actions. I/O belongs in the watcher or the config loader.
- **Worst finding wins, all reasons survive.** A verdict with no stated reason
  is useless to the human reading it.
- **Tests use stdlib `unittest`.** pytest is not installed and isn't worth a
  dependency yet.
- **Errors from external things** (Ollama down, config malformed) raise a typed
  error with a message the user can act on — never a raw traceback.
- **Runtime output is ASCII only.** The default Windows console encoding mangles
  em dashes, and this text goes to a terminal during the demo. Em dashes in
  comments and docstrings are fine; anything inside a `print()` is not.
- **Two different kinds of "ignore".** The config's `deny` list is *policy* — the
  agent should not be writing there. `IGNORED_DIRECTORIES` (now in
  `action_monitor/tree.py`, shared by the watcher and the session log) is
  *signal-to-noise* — don't even look. `.git` is in both, for different
  reasons: an agent running `git commit` rewrites it constantly, and reporting
  that would bury the events that matter. Don't merge them.
- **Policy can live in the watched repo.** `resolve_config_path(root)` prefers
  `<watched-repo>/sentinel.config.json` and falls back to Sentinel's own. That
  is what makes "assign Sentinel to a project" mean anything — each project
  carries its own rules, and they travel with it.
- **The session log is the only thing that writes outside Sentinel's repo**, and
  it writes only to `<watched-repo>/.sentinel/session/`. That path is gitignored
  *and* in `IGNORED_DIRECTORIES`, so Sentinel never observes its own writes.
  Everything else in the watched tree is opened read-only.
- **A baseline snapshot is taken at watcher start, not on first event.** watchdog
  fires *after* a write lands, so by the time an event arrives the original
  bytes are gone. There is no other way to have a real "before" without adding
  a git dependency.
- **Sentinel never acts.** A BLOCKED verdict means "this should not have
  happened", not "Sentinel stopped it". Nothing in this repo reverts, deletes,
  or blocks anything (instructions.md #6). `GitHubClient` is read-only and a
  test asserts it has no write methods — keep it that way.
- **All GitHub calls go through `sentinel/shared/github.py`.** Never call the
  API from a sub-agent directly (instructions.md #4).
- **Config file handling is shared** in `sentinel/shared/config_file.py`. Each
  component owns its own *section* and dataclass, but the file reading, the
  `_comment` convention, and the error messages live in one place.
- **Three vocabularies, on purpose.** `Verdict` (ALLOWED/FLAGGED/BLOCKED) answers
  "was this action permitted". `RiskLevel` (LOW/MEDIUM/HIGH) answers "how likely
  is this change to hurt". `ReliabilityVerdict`
  (SAFE/REVIEW/CONDITIONAL/STOP) is the orchestrator's answer on the whole
  change. Don't collapse them. **Norms deliberately reuse `Verdict`** rather
  than adding a fourth — "was this permitted" is exactly what a norm asks.
- **Two kinds of norm, and the difference is load-bearing.** A norm with
  `patterns` is checked by regex: deterministic, free, offline. A norm with only
  a `statement` is judged by the model, because no regex catches "don't redesign
  the UI without asking". Both produce a `NormFinding` tagged with `source`
  (`pattern` or `model`), and that tag is always shown — a regex hit is a fact,
  a model's judgement is an opinion, and they must not read identically.
- **The model reports whether a norm broke; the human's config says what it
  costs.** Severity lives in `sentinel.config.json`, written by a person. The
  model is never asked how serious anything is, and `_verify()` drops any
  finding naming a file that was not in the change or a norm id that does not
  exist. That is what keeps `synthesize()` deterministic even with a model in
  the loop.
- **Explanation is narration, never judgement.** `explainer/explain.py` describes
  what is in the diff and is explicitly forbidden from saying whether a change
  is safe. That answer comes from `synthesize()`, and moving it into the model
  would break the rule below.
- **The deterministic answer always exists.** `diff.summarise()` answers "what
  changed?" with no model at all, and every narration path falls back to it.
  `--plain` doesn't even import Strands. If a model call can lose you the
  answer, the design is wrong.
- **Question routing is keyword matching, not a model call.** `query.classify()`
  is pure and deterministic: "can we ship?" reaches the verdict identically on
  every run and costs nothing. Only a question matching no intent reaches a
  model, and it is handed the *finished* `Assessment.report()` — never the raw
  signals — so it can restate but cannot decide. That closes CLAUDE.md's
  "do not let the bot's LLM answer 'can we ship?'" trap structurally.
- **The cost guard on the hosted endpoint is not optional.** It spends a fixed
  credit and anyone with the URL can call it, so prompt size, token count and
  call rate are capped in `agentcore_app.py` before anything reaches Bedrock.
  Rate limiting is per install id and in-process: a spend guard, not a security
  control. Anything stronger belongs in the proxy in front of it.
- **`query.answer()` is the Phase 5 seam.** Interfaces call it and nothing else,
  so hosting the orchestrator on AgentCore changes that one function, the same
  way `llm.py` isolates the model provider. Don't let an interface reach past it
  into `review_session()` directly.
- **Signals are gathered per intent, not all at once.** Asking "what did it do?"
  must not pay for a norm check. Keep it that way — the token cost of the chat
  interface is the thing that decides whether people use it.
- **The bot can start Sentinel, and only Sentinel.** `/watch` and `/unwatch`
  over Telegram start and stop *Sentinel's own watcher*, so a session kicked off
  from a phone is still supervised. That is the same distinction the dashboard's
  Start button relies on - Sentinel acting on itself, at an allow-listed human's
  request - and nothing there touches the watched code. Control commands are
  matched **exactly** and handled *before* `query.answer()`, because
  `classify()` is keyword matching and a question containing "watch" must never
  spawn a process. `/start` is deliberately not a command: Telegram puts a Start
  button in front of every new chat.
- **The bot token lives in `~/.sentinel/telegram.json`, mode 0600.** Every other
  setting in this project lives in `sentinel.config.json`; that file is checked
  into git and this one is a secret. It is validated by `getMe` before it is
  stored, and the page only ever receives its last four characters.
- **Closing the dashboard does not stop what it started.** There is no
  `stop_all()`. The whole point of the bot is walking away, so watchers and the
  bot outlive the page; the dashboard says what is still running instead, on
  both Ctrl-C and `kill`.
- **The Telegram allow-list is a security boundary, not a preference.** Answers
  quote real source lines out of a private repo, and anyone can find a bot by
  name. An empty allow-list means *answer nobody*; never flip that to "answer
  everyone" for convenience. A stranger gets silence, not a refusal — a refusal
  confirms the bot exists and is worth probing.
- **Git hooks never block.** Every installed hook ends `exit 0` unconditionally,
  and a test asserts no hook contains `exit 1`. A pre-push hook returning
  non-zero *aborts the push*, which would make Sentinel act on the codebase —
  the one thing it promises not to do. Installation also refuses to overwrite a
  hook Sentinel did not write; a project's existing pre-commit tests matter more
  than our reporting.
- **The MCP server's whole surface must stay read-only.** It is the one
  interface a coding agent drives directly, so a write tool there would hand an
  agent the ability to act through Sentinel. A test fails if any tool name
  contains `fix`, `write`, `revert`, `apply`, `block` and friends. `/fix` style
  output is *text for a human*, never an action.
- **Every agent consultation is logged and reported to the human.** An agent
  that can ask its supervisor and quietly fix what it finds is an agent whose
  mistakes nobody sees, and one optimising to pass Sentinel rather than to write
  good code. `record_consultation()` plus `consultation_summary()` keep the
  human oversight of a loop they are no longer inside. Consultations are
  *informational* — they never change the verdict, so `synthesize()` stays pure.
- **The dashboard configures and starts Sentinel, but never decides anything.**
  It used to be a pure viewer; it is now the surface you drive Sentinel from,
  because making people learn three terminal commands before the interface
  shows them anything was backwards. What did *not* change: `export.py` still
  only serialises what `review_session()` decided, every answer still comes
  from `query.answer()`, and every rule is written through
  `wizard.write_norms`. It drives the same functions the terminal drives.
- **"Sentinel never acts" survives, and the distinction is load-bearing.** It
  means Sentinel never reverts, blocks, deletes or commits *the code it is
  watching*. Starting a watcher and writing `sentinel.config.json` are Sentinel
  acting on **itself**, at the explicit click of the repo's owner. Removing an
  agent deletes nothing inside the folder, and a test pins that.
- **Localhost is not a boundary once there are POST routes.** Any page in
  another browser tab can reach `127.0.0.1`. `interfaces/guard.py` requires a
  per-session token (minted at start, injected into the page), rejects a
  foreign `Origin`, requires `Content-Type: application/json` on mutations, and
  resolves every path before deciding anything about it. The token is a CSRF
  defence, not authentication. Do not add a mutating route that skips it.
- **The live document must never call the model.** The dashboard rebuilds it
  every few seconds; judging the written norms takes about a minute, so
  `build_document` defaults to `use_model=False` and names the skipped norms in
  `unchecked` - which caps the verdict at REVIEW and is exactly what the reader
  should be told. The rules check is a *button*, and it runs them. This is
  "signals are gathered per intent" applied to a timer.
- **One config writer, two front ends.** The GUI and the wizard both compose
  through `build_norms()` and write through `write_norms()`, so a project set
  up in the page and one set up in the terminal are byte-identical. A test pins
  it. Two writers would drift within a week.
- **The watcher is started as the CLI entry point, not a second code path.**
  `supervisor.start()` spawns `python -m sentinel.action_monitor.watcher`, so
  the GUI and the terminal cannot behave differently, and the CLI panel shows
  literally what the buttons run.
- **An agent's state is verified, or it says it is not.** `supervisor.status()`
  will not report RUNNING from a pid it cannot prove is ours - pids get reused,
  and reporting a text editor as a running agent is worse than reporting
  nothing. `UNVERIFIED` is a real answer, the same instinct as "an unverified
  check is never a pass".
- **`dashboard.py` binds to 127.0.0.1 and must stay there**, and that is now
  the *second* lock rather than the only one - see `guard.py` above.
  Historically: The document quotes
  real source lines out of a private repo and the server has no authentication
  at all - the same reasoning as the Telegram allow-list, applied to a port.
- **Colour in the dashboard means status.** The four verdict levels are the only
  saturated colours; the violet accent is for interactive affordances only. If
  something new needs colour, it probably needs a status instead.
- **Use `tests/offline.py`'s `go_offline()` in any test that reaches
  `review_session()`.** Twice now the suite was only fast because Ollama
  happened to be down - it went 0.6s -> 83s, then 0.6s -> 23s. A green run must
  never depend on a service being switched off.
- **A deletion is usually not final, and saying so is not acting.** The baseline
  snapshot still holds every file as it was, so `recoveries()` offers the exact
  `cp` command. Both paths are absolute deliberately — a relative destination
  restores into whatever directory the terminal happened to be in. Sentinel
  prints the command; the human runs it (instructions.md #6).
- **`code_risk/churn.py` is the only module that shells out to git**, and only
  ever `git log`. Local churn is computed rather than left as a standing
  caveat, because a warning that appears on every clean change teaches people
  to ignore all of them. Note a local session still cannot reach SAFE — CI is
  unknown until the change is pushed, which is the rule working, not a gap.
- **`--exit-code` is the human's pipeline acting, not Sentinel.** It returns 1
  on STOP so a CI job *configured by a person* can gate on it. Sentinel still
  only reports; `GitHubClient` stays read-only and nothing here posts anything.
- **Markdown and `report()` have different audiences.** `report()` is read in a
  terminal and stays ASCII; markdown is read on GitHub and may use tables. Both
  must keep "could not verify" as prominent as the verdict — collapse multi-line
  reasons in markdown, because a bare newline ends a list item.
- **A preset is a shortcut through the catalog, never a separate hidden set.**
  `PRESETS` names norm ids, and a test fails if any names one that does not
  exist — a typo would otherwise silently produce a smaller rule set.
- **`check_my_work` deliberately skips the model-judged norms.** A minute-long
  call is useless inside an agent's edit loop. It says exactly which rules it
  did not check and that this is "not a clean bill of health" — an agent told
  "no violations" will report itself done.
- **THE LLM NEVER DECIDES THE VERDICT.** This is the most important rule in the
  repo. `verdict.synthesize()` is deterministic: identical signals give an
  identical verdict on every run. `agent.py` only restates a verdict that has
  already been decided, and its system prompt forbids changing it or inventing
  findings. A supervisor that answers SAFE on one run and STOP on the next for
  the same input is not a supervisor. If Phase 4 or 5 needs the model to "decide"
  something, that is a signal to add a rule to `synthesize()` instead.
- **An unverified check is never a pass.** Anything Sentinel could not read goes
  in `unchecked` and caps the verdict at REVIEW — a clean-looking SAFE while we
  know we did not look is the worst thing this tool could do. `decisions=None`
  (nobody was watching) is deliberately different from `decisions=[]` (watched,
  saw nothing wrong).
- **A check that couldn't run is not a pass.** When a signal is unavailable it
  goes in `ChangeRisk.unchecked` and is printed as "could not check", never
  silently omitted (instructions.md #7).
- **Test the heuristics offline.** `assess_file`/`assess_change` take data, not
  a client, so they test without a network. Anything needing GitHub goes in
  `tests/live_pr_check.py`, outside the suite — a suite that fails because
  GitHub is slow teaches the team to ignore failures.

## Things that bit us — don't repeat

- `lstrip("./")` strips a *character set*, so it turns `.env` into `env` and
  silently defeats every dotfile rule. Use `posixpath.normpath`.
- `sys.stdin.isatty()` is not reliable across the shells the team uses; it
  returned True under redirected stdin on Git Bash. Catch `EOFError` around
  `input()` as the real guard.
- `qwen3:4b` calls tools correctly but often returns an empty final message
  (thinking-mode quirk). Prefer `qwen3.5:4b` or `qwen3:8b` where the final
  text matters, e.g. the orchestrator's verdict.
- **Models actually verified end to end** (Ollama on the dev Mac), so nobody has
  to rediscover this: `gemma4:12b-mlx` handles narration *and* the strict-JSON
  norm judgement cleanly, no code fences, no truncation. `deepseek-r1:14b` also
  returned valid JSON on a small diff in ~27s despite being a reasoning model —
  the 4096 `max_tokens` in `get_model()` absorbed its thinking. That headroom
  shrinks as the diff grows, so if statement norms start landing in `unchecked`
  on a big change, raise `max_tokens` before suspecting the prompt.
- `resolve_model_id()` prompts interactively when several models are installed,
  which fails in any non-interactive context. **Set `SENTINEL_MODEL` for scripts,
  bots and demo recordings** — `installed_models()` already filters out
  embedding models, so a pulled `nomic-embed-text` will not be offered.
- Keyword globs match anywhere in a path, so `*admin*` scored
  `admin/css/widgets.css` as "permissions code". Assets and docs are now exempt
  via `code_risk.ignore_extensions`. **Test every new keyword against a real
  repo before trusting it** — this was invisible until we ran it on
  django/django.
- The same bug, three more times, in the norm catalog — all caught by
  `--audit` before shipping, none of them visible from reading the regex:
  `AKIA[0-9A-Z]{16}` matched 15 copies of AWS's documented `AKIAIOSFODNN7EXAMPLE`
  placeholder in botocore's fixture JSON; `secret_key = "..."` matched five
  constants holding the *name* of an env var (`SECRET_KEY = 'aws_secret_access_key'`),
  which is the correct pattern and the opposite of a hardcoded secret; and a
  bare `verify=False` matched numpy's `array_function_dispatch(..., verify=False)`,
  which has nothing to do with TLS — and that norm is BLOCKED, so it would have
  produced a **false STOP**. `tests/test_norms.py` now pins every one of these
  as a "must not match" case. **Run `--audit` against a real repo before
  trusting a new pattern.**
- A norm whose statement overstates what its regex detects loses the reader's
  trust. "No silently swallowed errors" matched every bare `except:` in
  sqlalchemy, 27 of them, most being legitimate re-raises. It is now called
  "no unnamed error catching", which is what the pattern actually finds.
- Build the prompt *inside* the try block that guards the model call. Twice we
  wrote `_narrate(PROMPT, build_it(), fallback)`, where a failure in `build_it()`
  escapes the guard entirely and takes the answer with it. `_narrate` now takes
  a callable for exactly this reason.
- **Editors and tools save atomically** — write a temp file, rename over the
  target — which fires a real `deleted` event for a file nobody deleted. Sentinel
  reported "the agent deleted sentinel/interfaces/query.py" while that file sat
  right there, caught by watching Claude Code work on this repo. `decisions()`
  now drops a DELETE whose path exists again. A genuine deletion leaves nothing
  behind, so it survives; a delete-then-recreate nets out to a modification the
  explainer already describes correctly. **Note `FileChange.status` was already
  right** — it is derived by comparing content, not by trusting the event type,
  which is exactly why that design was chosen.
- Don't assert "no model was involved" by grepping the answer for the word
  "model" — it appears in the norm id `no-hardcoded-model-ids` and in the
  evidence line. Probe the property instead: take the model away and assert the
  output is byte-identical.
- A `unittest.mock` `return_value` of a `BytesIO` is consumed by the first call.
  Anything that retries or chunks (Telegram's 4096-character split) needs
  `side_effect=lambda *a, **k: fresh_response()` or it fails on the second send.
- **The suite was only offline because Ollama happened to be down.** Tests that
  used `Path(".")` were asking questions about *Sentinel's own checkout*, which
  has a live session in it — so the first time a model was running, the suite
  went from 0.4s to **83s** and started depending on a service. Never point a
  test at `Path(".")`; use a temp repo. `tests/test_query.py` now forces the
  Strands import to fail in `setUp` so being offline is enforced rather than
  hoped for. A green run must not depend on something being switched off.
- `os.path.basename` only knows the separator of the machine it runs on, so it
  will not split `C:\...\git.exe` on macOS. Anything parsing a path that came
  from *another* machine (here, a teammate's `ps` output) has to handle both
  separators by hand.
- GitHub's `per_page` caps the churn count, so "30 commits" meant "the page was
  full", not "exactly 30". Anything derived from a capped API response must say
  "at least N".
- Reasoning models spend tokens *thinking* before answering, and that counts
  against `max_tokens`. The default was too small, so the model ran out
  mid-sentence and the answer was lost — `MaxTokensReachedException`.
  `get_model()` now asks for 4096. If a new model truncates, raise it there.
- **`--explain` is slow on a laptop** (minutes, not seconds) for the same
  reason. The deterministic verdict prints instantly; only the narration waits.
  For a demo recording, run the verdict and the explanation as separate takes,
  or use the smallest model available. Measured on the dev Mac with
  `gemma4:12b-mlx`: **~42s** to narrate a 9-file change, **~1m46s** for a full
  `session_review` that also judges statement norms. Plan the demo around those
  numbers — do not discover them while recording.
- **A backgrounded process ignores SIGINT**, so `kill -INT` in a script proves
  nothing about Ctrl-C. A shutdown test that "passed" that way was measuring a
  dashboard that had never shut down. `dashboard.py` now handles SIGTERM too, so
  `kill` and Ctrl-C behave identically - which matters more since watchers
  outlive the page.
- **A Fibonacci sphere puts its first and last points exactly on the poles**,
  where the ring radius is zero. With four folders that put two hubs straight
  above and below the centre and the whole 3D structure drew as a thin vertical
  ribbon. The fix is the standard half-step offset, `(i + 0.5) / n` rather than
  `i / (n - 1)`, and a test now pins that no point has near-zero horizontal
  spread.
- **A viewBox sized for the worst case leaves the usual case floating in an
  empty frame.** The 3D view is framed for a node at full radius held exactly
  side-on, which almost never happens, so it opens at zoom 1.55 instead of 1.
  Overflow is visible rather than clipped, so an unusually wide structure still
  reads.
- **A polling UI turned a one-minute model call into a per-poll cost.** The
  dashboard rebuilt its session document every four seconds, and
  `review_session()` judged the written norms every time - about 1m46s of
  inference, queued forever, and the page never loaded. Anything on a timer
  must pass `use_model=False`. The general rule: before putting an existing
  function behind a refresh loop, ask what it costs *per call*.
- **A subprocess with `cwd` set to the watched repo could not import
  `sentinel`.** The parent only found the package because it was launched from
  the checkout. The watcher started, died instantly, and was reported as
  "running". Fixed twice over: no `cwd` override, and the package's parent is
  put on the child's `PYTHONPATH`. **And `start()` now waits ~0.6s to see if the
  child survived** - being told "running" about a process that never ran is the
  worst possible answer from a supervisor.
- **The status strip said "watching" whenever it was served live**, even with
  the agent stopped, because `live` (served by the local server) had been
  conflated with `watching` (a process is actually running). In a supervision
  tool that is the one lie that matters. They are separate props now.
- **A fixed-height panel inside a scrolling flex column still collapses**,
  because flex items default to `flex-shrink: 1`. The file tree vanished
  entirely. `flex: none` on `.workspace`.
- **Patching `sentinel.norms.checker.check_statements` does not affect
  `review.py`**, which imported the name directly (`from ..norms.checker import
  check_statements`). The mock did nothing, a real Ollama call got into the
  suite, and the run hung past 120s. Patch the name *where it is called*, and
  put `go_offline()` in `setUp` as the guard that actually holds.
- **There is no `strands-agents[bedrock]` extra.** `BedrockModel` ships in the
  core package and boto3 comes with it, so the remedy in an error message is
  `pip install -r requirements.txt`. An install instruction that does not work
  is worse than none - the user follows it and then distrusts the next one.
- `mock.patch.dict(os.environ, {"X": None})` raises `TypeError: str expected,
  not NoneType`. To *remove* variables for a test, pop and restore them by
  hand; there is no None-means-unset in that helper.
- `llm.py` used to import `OllamaModel` at module scope, which made the whole
  module unimportable on a machine that only ever talks to the hosted runtime.
  Every provider import is now inside the function that needs it.
- **`deploy/Dockerfile` has never been built.** The Docker daemon was not
  running on the dev Mac when it was written. The service it runs *is* verified
  - `agentcore_app` was started locally and driven end to end through
  `remote.ask` over HTTP - but expect to iterate once on the image itself.
- Strands `Agent` streams its reply to stdout by default, so `explain()` printed
  its answer *and* returned it — duplicated output. Pass `callback_handler=None`
  when you want the text returned rather than printed.

## How a verdict is reached

```
Action Monitor  ->  list[Decision]     \
Project norms   ->  list[NormFinding]   \
Code Risk       ->  ChangeRisk           >  synthesize()  ->  Assessment
GitHub checks   ->  CIStatus            /   (deterministic)      |
                                       /                         v
Session log     ->  list[FileChange]  -+--> explainer          agent.explain()
                                            (LLM, narration)   (LLM, narration)
```

The ladder, worst signal wins:

| Signal | Verdict |
| --- | --- |
| A BLOCKED action, a BLOCKED norm broken, or CI failing | STOP |
| HIGH risk, nothing blocked | CONDITIONAL (+ conditions to satisfy) |
| A FLAGGED action, a FLAGGED norm broken, MEDIUM risk, or CI pending | REVIEW |
| Nothing flagged, no norms broken, low risk, CI green, everything checked | SAFE |
| Anything at all could not be checked | never better than REVIEW |

`None` and `[]` mean different things for both `decisions` and `norm_findings`:
`None` is "nobody checked" and caps the verdict at REVIEW; `[]` is "checked,
nothing found". A project with **no norms configured** should be passed `[]` —
there was nothing to check, so nothing was missed. `review_pull_request` passes
`None` only when norms exist and it genuinely cannot evaluate them, since
GitHub gives it a file list rather than the diff text.

## Where Phase 6 picks up

Phase 5's **code** is done: three providers behind `run_agent()`, the hosted
service in `interfaces/agentcore_app.py`, its container in `deploy/`, and 41
tests in `tests/test_aws.py`. Nothing about Bedrock or AgentCore remains to be
written.

What remains is account work, and it is written up step by step in
`docs/deploy-agentcore.md`: model access, an ECR push, a runtime, and the auth
decision. **Bedrock model access is the long pole** - request it before reading
the rest.

Two things to know before deploying:

- `deploy/Dockerfile` is unbuilt (no Docker daemon on the dev machine). The
  service inside it is verified; the image is not.
- The AgentCore control-plane CLI is new and its shape has been moving. The
  console asks for the same four things - image URI, role, network mode,
  environment - so use it if the CLI has drifted.

## Where Phase 4 picked up

Phase 4 is the natural-language query interface: "What went wrong?", "Why did
you stop it?", "Can we ship?", answered from current state, then a Telegram or
WhatsApp bot on top (pick **one**, per tasks.md).

What is already there to build on:

- **`review_session(root)` is the function the bot should call.** It returns
  `(Assessment, changes, findings)` for a watched repo, entirely from disk, with
  no network. That is "What went wrong?", "Why did you stop it?" and "Can we
  ship?" already answered — the bot's job is transport, not analysis.
- `explainer.explain_session()` answers "what did it just do?", and
  `diff.summarise()` answers it without a model when one isn't available.
- `checker.correction_request()` already composes the "please fix this" message.
  **Printing it is done; sending it is not, and must not be** — a bot that
  messages the coding agent is Sentinel acting, which instructions.md #6
  forbids. A human sends it.
- `Assessment` carries everything a query needs — verdict, reasons, conditions,
  unchecked — and `report()` already renders it as plain text.
- `agent.explain()` shows the pattern for talking to the model: build the prompt
  from an already-decided `Assessment`, `callback_handler=None`, and fall back
  to the deterministic text when the model fails.
- The queries are read-only, so they need no new permissions. Sending a message
  is a real side effect and needs a human in the loop (instructions.md #6).

The trap to avoid: do **not** let the bot's LLM answer "can we ship?" from raw
signals. It answers by reading the `Assessment` that `synthesize()` produced.
