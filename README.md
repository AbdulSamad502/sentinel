# Sentinel

**A supervisory agent for AI coding agents.**

Sentinel does not write code. It watches an AI coding agent — Claude Code,
Cursor, Copilot Workspace — while that agent works on a codebase, and answers
one question continuously:

> Is what this agent is doing safe, correct, and shippable?

## The problem

Developers increasingly hand real work to AI coding agents. Those agents can
touch files they shouldn't, delete or overwrite things silently, claim "done"
when the change doesn't work, make security-sensitive changes without review,
and drift off what was actually asked. The person nominally supervising is
often not watching closely enough to catch it.

Sentinel sits alongside the coding agent as an independent, skeptical observer
and gives a clear verdict with reasons:

| Verdict | Meaning |
| --- | --- |
| **SAFE** | Nothing concerning. Ship it. |
| **REVIEW** | A human should look before this merges. |
| **CONDITIONAL** | Fine *if* the named conditions are met. |
| **STOP** | Do not proceed. Something is wrong. |

Sentinel **observes, analyses, and reports**. It does not act on your codebase.

## Components

1. **Action Monitor** — watches the coding agent's file writes, deletes,
   dependency changes and git commands; enforces path allow/deny lists; flags
   dangerous actions as they happen.
2. **Code Risk Analyzer** — reviews changed files for security-sensitive paths,
   missing tests, unusually large diffs, and high file churn.
3. **Risk Correlation + Reliability Verdict** — the orchestrator, which fuses
   the signals above with CI/test status into one verdict and plain-English
   reasons.
4. **Natural-Language Control** — ask it things like *"What went wrong?"*,
   *"Why did you stop it?"*, *"Can we ship?"* from the dashboard, a terminal,
   or Telegram.
5. **Change Explainer** — answers *"what did it just do?"* in plain English,
   from what Sentinel independently watched happen.
6. **Project Norms** — the rules *your* project holds its agent to, written in
   plain English and checked on every change.
7. **Agent Consultation** — the coding agent can ask Sentinel for the rules
   *before* it writes, over MCP. Every question it asks is reported to you.

## Architecture

```
Clients:  CLI watcher + git hooks  ·  Telegram bot  ·  dashboard  ·  MCP (the coding agent)
                     |
Orchestrator:  the verdict, computed deterministically, on your machine
                     |
run_agent()  ->  one of four model providers, chosen by SENTINEL_PROVIDER
                     |
  ollama          bedrock           groq            agentcore
  a model on      AWS Bedrock,      Groq's API,     a container you deploy
  this laptop     your credentials  your key        to Bedrock AgentCore
```

The model provider lives behind a single seam in
[`sentinel/llm.py`](sentinel/llm.py), so switching between the four is one
environment variable. Every interface goes through one function,
`query.answer()`, for the same reason.

**The verdict is never the model's to make**, on any provider. It is computed
by `synthesize()`, which is deterministic — and a test runs the same session
through all four providers and requires the report to come back byte-identical.
The model narrates what was decided; it does not decide.

### Where your code goes

A tool that reads your code should tell you where it sends it. `ollama` is the
default, and on it nothing leaves your machine.

| `SENTINEL_PROVIDER` | The model runs | What leaves your machine |
| --- | --- | --- |
| `ollama` *(default)* | on your laptop | nothing |
| `bedrock` | in **your own** AWS account | diffs, to an account you control, under your IAM role |
| `agentcore` | in a runtime **you** deploy | diffs, to that runtime — never stored, never used for a verdict |
| `groq` | Groq's API | diffs, to a third party |

What is sent, on the three non-local providers, is the diff of what your coding
agent just changed — so the change can be narrated and judged against the
project rules you wrote. Two things bound it:

- **Secret-bearing paths are withheld.** A change to `.env`, `*.pem`, `*.key`,
  or anything matching `*secrets*` / `*credentials*` is reported to the model as
  *having changed*, with the values held back. The verdict still sees it in full
  — `protected_paths` decides what touching `.env` means, offline, from the
  change record. `tests/test_egress.py` fails if a secret ever reaches a prompt.
- **One call is capped** at 60,000 characters, for every provider, in
  `run_agent()`.

If that trade is not one you want, `SENTINEL_PROVIDER=ollama` and none of it
happens. The verdict is identical either way — that is the whole point of
keeping it out of the model's hands.

Note the two directions. The **watcher and git hooks** feed observations *in*;
the **Telegram bot** answers the human, and **MCP** answers the coding agent
itself. Only observations flow into Sentinel, and only text flows out — nothing
in any direction can change your code.

## Setup

Requires Python 3.11+. [Ollama](https://ollama.com) runs the model locally, and
Node 20+ builds the dashboard.

```bash
git clone <repo-url> && cd Sentinel
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Pull at least one chat model if you don't have one:

```bash
ollama pull qwen3:4b
```

Then run the setup check:

```bash
python tests/hello_agent.py
```

On first run it lists the models Ollama has installed on your machine and asks
you to pick one — teammates run different hardware, so the choice is
per-laptop and remembered in a gitignored file. To skip the prompt (CI, bots,
or just preference), set the model explicitly:

```bash
SENTINEL_MODEL=qwen3:4b python tests/hello_agent.py
```

### Running on a hosted model instead

Nothing above needs an account anywhere. To use a hosted model, set the provider
and its model id — no other code or config changes:

```bash
export SENTINEL_PROVIDER=bedrock
export SENTINEL_BEDROCK_MODEL=<a model id your account can invoke>
```

```bash
export SENTINEL_PROVIDER=groq
export SENTINEL_GROQ_MODEL=<a current Groq model id>
export GROQ_API_KEY=<your key>
```

The Groq key is read from the environment only, never from `sentinel.config.json`
— that file is committed, and a secret has no business being offered a home in
it. `agentcore` points this machine at a container you have deployed, via
`SENTINEL_AGENTCORE_ENDPOINT`. Whichever you pick:

```bash
python -m sentinel.doctor .        # says which provider you are on, and what is missing
```

AWS account and Bedrock setup are in [docs/aws-setup.md](docs/aws-setup.md);
building and deploying the runtime container is in
[docs/deploy-agentcore.md](docs/deploy-agentcore.md).

## Watching a repo

The Action Monitor works today. Point it at the repo your coding agent is about
to work on:

```bash
python -m sentinel.action_monitor.watcher /path/to/your/repo
```

It snapshots the tree first — that is what lets it explain the change
afterwards, since a filesystem event arrives *after* the write has landed and
the original bytes are already gone. Then it reports every action as it happens:

```
[ok  ] create src/feature.py
[STOP] create .env
         - '.env' is a protected file (matches '.env')
[FLAG] create requirements.txt
         - 'requirements.txt' changes project dependencies - needs a human to confirm
[FLAG] delete src/main.py
         - 'delete' actions are set to FLAGGED in this repo's config
```

What counts as protected, denied, or dangerous is entirely data — edit
[`sentinel.config.json`](sentinel.config.json), not Python. Making Sentinel
stricter or looser should never require a code change.

`STOP` means *this should not have happened*, not *Sentinel stopped it*. It
reports; you decide.

## Scoring a change

The Code Risk Analyzer scores a pull request on four cheap, explainable
signals: does it touch sensitive areas (payments, auth, permissions), is the
file churning, does it change source without touching a test, and is the diff
unusually large.

```bash
python tests/live_pr_check.py django/django
```

```
Risk: HIGH across 3 changed file(s)
  HIGH: tests/admin_widgets/tests.py - touches permissions code; changed in at least 30 recent commits - this file is unstable
```

Set `GITHUB_TOKEN` to raise GitHub's rate limit from 60 to 5000 requests an
hour. A signal that couldn't be checked is reported as *could not check* — it
is never quietly treated as a pass.

## The verdict

The orchestrator combines what the Action Monitor saw, what the Code Risk
Analyzer scored, and whether CI passed, into one answer:

```bash
python tests/demo_scenario.py
```

```
A coding agent was asked to add a discount code field to checkout.
Sentinel watched what it actually did.

  BLOCKED: modify .env - '.env' is a protected file (matches '.env')
  BLOCKED: git command: git push --force origin main - git command contains 'push --force', which can destroy work that isn't recoverable
  FLAGGED: delete src/checkout/legacy_pricing.py - 'delete' actions are set to FLAGGED in this repo's config
  FLAGGED: modify requirements.txt - 'requirements.txt' changes project dependencies - needs a human to confirm
  ALLOWED: modify src/checkout/discount.py
  ALLOWED: create tests/test_discount.py

STOP - Do not ship this as it stands.

Why:
  - the agent took 2 action(s) it should not have: modify .env; git command: git push --force origin main
  - 2 action(s) need a human's eyes: delete src/checkout/legacy_pricing.py; modify requirements.txt

Could not verify:
  - CI status (could not be read)

  An unverified check is not a pass.
```

Or against a real pull request:

```bash
python -m sentinel.orchestrator.review django/django 21793
```

Add `--explain` to have the local model restate it in plain English.

**The verdict is deterministic.** Identical signals always produce an identical
verdict — the language model only puts it into plain English, and Sentinel
still works when the model is unavailable. A supervisor whose answer changes
between runs on the same input isn't a supervisor.

**An unverified check is never a pass.** If Sentinel couldn't read CI, or wasn't
watching when the agent worked, it says so and refuses to call the change SAFE.

## What did it just do?

A coding agent can rewrite twelve files in ninety seconds. Reading that by hand
is slow, and asking the agent to summarise its own work costs tokens and asks
the suspect to write the report. Sentinel was watching independently, so it can
tell you itself:

```bash
python -m sentinel.explainer.explain /path/to/your/repo
```

```
3 file(s) changed, +12 -4

  modified src/checkout/discount.py (+7 -2)
  added    tests/test_discount.py (+5)
  deleted  src/checkout/legacy_pricing.py (-2)

What the agent did:

It added an optional discount code to the checkout total, looking the code up
and subtracting it before returning. It removed the legacy pricing module
entirely, and added a test for the new discount path.
```

Everything above the narration is computed, not generated — so if the model is
unavailable, you still get the file list and the line counts. `--plain` skips
the model entirely and spends no tokens; `--detail src/checkout/discount.py`
explains one file in depth.

## Your project's own rules

Every project has rules no linter knows: *no hardcoded model ids*, *don't make
frontend design decisions without asking*, *never widen a CORS policy*. Tell
Sentinel what yours are when you point it at the project:

```bash
python -m sentinel.norms.wizard /path/to/your/repo
```

It offers a catalog of common rules, then asks for the ones only your team
knows. The answers are written to that project's own `sentinel.config.json`, as
data you can edit afterwards. Rules travel with the project.

Some rules a regex can check exactly, and those are checked deterministically,
offline, for free. Others — *"don't redesign the dashboard without asking"* —
only a model can judge, so those go to the model. Either way, **the model only
reports whether a rule was broken. How much that matters is set by you, in the
config.** A finding always says which found it:

```
Project rules broken:
  - src/checkout.py:1 breaks 'no-hardcoded-model-ids' (found by pattern): MODEL = "gpt-4o"
  - src/checkout.py:4 breaks 'no-debug-output' (found by pattern): console.log("debugging total")
```

Then ask whether it can ship, and get a message you can paste back to the agent:

```bash
python -m sentinel.orchestrator.session_review /path/to/your/repo --fix-prompt
```

Sentinel writes that message. **You** send it — Sentinel has no channel to the
coding agent, by design. It observes, analyses and reports.

Only lines the change *added* are ever blamed, so you are never flagged for
problems you inherited. Every built-in pattern has been audited against real
codebases (botocore, sqlalchemy, numpy, flask, requests, urllib3), and each
false positive that found is pinned as a test. Before trusting a rule of your
own:

```bash
python -m sentinel.norms.checker --audit /path/to/some/repo
```

## Let the agent ask *before* it writes

Catching a mistake is second best. The best case is the coding agent knowing the
rules up front — and it can, because Sentinel speaks **MCP**, which Claude Code,
Cursor and Windsurf all support natively:

```json
{"mcpServers": {"sentinel": {
    "command": "python",
    "args": ["-m", "sentinel.interfaces.mcp_server", "/path/to/your/repo"]}}}
```

The agent gets three read-only tools. The first is the one that matters:

| Tool | What it gives the agent |
| --- | --- |
| `sentinel_project_rules` | your rules and your protected files, **before** it writes a line |
| `sentinel_what_changed` | its own diff, for re-orienting in a long session |
| `sentinel_check_my_work` | rules it has broken, before it claims to be done |

Sentinel still never acts. Every tool is read-only; the agent chooses to ask and
chooses to fix.

**And you still see everything.** An agent that quietly self-corrects is an agent
whose mistakes you never learn about — so every consultation is logged and
reported next to your verdict:

```
1 file(s) changed in this session.

The coding agent consulted Sentinel 3 time(s): check_my_work x1, project_rules x1, what_changed x1.

Project rules broken:
  - src/checkout.py:1 breaks 'no-hardcoded-model-ids' (found by pattern): MODEL = "gpt-4o"
```

## Catching dangerous git commands

Filesystem events can't see a `git push --force`. Git hooks can:

```bash
python -m sentinel.action_monitor.githooks --install /path/to/your/repo
```

```
[sentinel] BLOCKED: git command: git push --force origin master - git command
           contains 'push --force', which can destroy work that isn't recoverable
[sentinel] Reporting only - this operation was not blocked.
```

**The push still went through.** Every hook exits 0 unconditionally — a hook that
returned non-zero would abort your push, and Sentinel does not act on your work.
It covers push, commit and rebase; `reset --hard` and `clean -fd` have no git
hook at all, so nothing can report them, and Sentinel says so rather than
implying coverage it doesn't have.

## Just ask it

You handed the agent a task and walked away. You don't want a report format —
you want to ask a question:

```bash
python -m sentinel.interfaces.query /path/to/your/repo "can we ship?"
```

| Ask | You get |
| --- | --- |
| *"What did it just do?"* | the change, described |
| *"Can we ship it?"* | the verdict, and why |
| *"Why did you stop it?"* | what went wrong, and what couldn't be checked |
| *"Did it follow my rules?"* | which project rules the change breaks |
| *"How do I fix it?"* | a message to send the coding agent |
| *"Are you watching?"* | session status |

Questions are routed by keyword — deterministically, for free. **"Can we ship?"
never costs a token and never answers differently on two runs**, because the
answer comes from the same computed verdict the CLI prints. Only a question that
matches nothing reaches the model, and even then the model is handed the finished
report and asked to find the answer in it. It can restate. It cannot decide.

### From your phone

```bash
export SENTINEL_TELEGRAM_TOKEN=...             # from @BotFather
export SENTINEL_TELEGRAM_ALLOWED_CHATS=12345   # your chat id
python -m sentinel.interfaces.telegram /path/to/your/repo
```

Or set it up from the dashboard's Telegram panel, which writes the same config
to `~/.sentinel/telegram.json` and starts the bot for you.

No new dependency — the Bot API is plain HTTPS JSON, so it uses the same stdlib
approach as the GitHub client.

**The allow-list is not optional.** These answers quote real source lines out of
your repo, and anyone can find a Telegram bot by name, so the bot only replies to
chat ids you name. Set none and it replies to nobody — it prints the chat id of
whoever messaged it, so you can add yourself and restart.

It replies; it never initiates. It sends no alerts, and it never messages your
coding agent — asking *"how do I fix it?"* returns the correction text **to you**.

## When it goes wrong

**A deleted file usually isn't lost.** Sentinel snapshots the tree before the
agent starts, so it can hand you the exact command back:

```
1 deleted file(s) can be restored from the session snapshot:

  src/checkout/legacy_pricing.py
    cp "/your/repo/.sentinel/session/before/src/checkout/legacy_pricing.py" "/your/repo/src/checkout/legacy_pricing.py"

Sentinel will not run these for you.
```

**See it in order** — useful when you step away and come back:

```bash
python -m sentinel.explainer.explain /path/to/your/repo --since 2h
```

```
14:32:07  [ok  ] modify src/checkout/discount.py
14:32:19  [    ] the coding agent asked project_rules
14:35:02  [STOP] modify .env
                     - '.env' is a protected file
14:36:44  [STOP] git push --force origin main
```

**Check your setup before you need it**, which is also the first thing to run on
a fresh clone:

```bash
python -m sentinel.doctor /path/to/your/repo
```

Every warning names its own remedy, and nothing that still works is called a
failure — Sentinel runs fine with no model, it just has less to say.

## In your pipeline

```bash
python -m sentinel.orchestrator.session_review /path/to/repo --markdown
python -m sentinel.orchestrator.session_review /path/to/repo --exit-code
```

`--markdown` gives you a pull-request comment to paste. `--exit-code` returns 1
on STOP so a CI job can gate on it — note that it's *your* pipeline config doing
the blocking. Sentinel reports; the GitHub client stays read-only and posts
nothing.

## The dashboard

The dashboard is the way most people use Sentinel. It keeps a registry of the
repos you supervise and starts and stops the watcher processes itself, so
nothing here needs a second terminal.

Build the frontend once, then start the server:

```bash
cd dashboard && npm install && npm run build && cd ..
python -m sentinel.interfaces.dashboard
```

Open **http://localhost:8765** and add the folder your coding agent is about to
work in. From the page you can start and stop watching, write and edit that
project's rules, run any of the CLI commands above, and set up the Telegram bot
— no config file to hand-edit and no command to remember. Pass a repo path to
add it on startup, or `--port` if 8765 is taken.

The verdict leads, then a file navigator beside the diff. The navigator behaves
the way Finder and Explorer both do — folders before files, disclosure
chevrons, arrow keys to move (Right opens, Left closes or jumps to the parent) —
so nobody has to learn it. Folders show the worst thing inside them, so a
problem never hides behind a collapsed chevron.

Colour means status and nothing else: the four verdict levels are the only
saturated colours on screen, so anything coloured is something that wants your
attention. *Could not verify* is given the same weight as the verdict itself.

The server binds to `127.0.0.1` only, and every mutating request must carry a
per-session token embedded in the page it served, a same-origin `Origin`, and a
JSON content type — loopback is not a boundary inside a browser, where any site
you visit can post to localhost. It is still not something to expose: the page
quotes real source lines out of your repo.

There is also a **Visualize** view that draws the session as a rotatable 3D
structure — the projection is about a hundred lines of TypeScript, with no 3D
dependency behind it.

## Tests

```bash
python -m unittest discover -s tests -t .
```

489 tests, no network, under a second. Anything requiring GitHub lives in
`tests/live_pr_check.py`, outside the suite. The dashboard has 39 of its own:

```bash
cd dashboard && npm test
```

Two of them are worth naming, because they pin the claims this README makes:
`tests/test_aws.py` runs one session through all four providers and fails unless
the report comes back byte-identical, and `tests/test_egress.py` fails if a
secret ever reaches a model prompt.

## Project status

Working software. Every component described above runs today, on a local model
with no account anywhere. [tasks.md](tasks.md) has the build history and what is
still open, [CLAUDE.md](CLAUDE.md) the scope, and
[instructions.md](instructions.md) the coding rules this repo holds itself to.

## License

[Apache 2.0](LICENSE)
