# Build the launch site for Sentinel

You are building a public marketing/landing site for a project called **Sentinel**. It will be the first thing hackathon judges see, so it has to look genuinely designed — not generated. Read this whole brief before writing any code.

---

## Step 0 — Install these skills first, before anything else

Install and use these three skills. Do not start designing until they are available:

1. **Framer Motion** — for all motion and transitions
2. **UI UX Pro Max Skill** — for layout, hierarchy, and design systems
3. **21st.dev** — for component quality and modern patterns

Use them together. The design bar for this site is "award-winning agency work," and I would rather you spend the time up front loading proper design guidance than improvise.

---

## What Sentinel is (you have no other context on this — read carefully)

**Sentinel is a supervisory agent for AI coding agents. It does not write code.**

It watches an AI coding agent — Claude Code, Cursor, Copilot Workspace — while that agent works on a codebase, and it answers one question continuously: **is what this agent is doing safe, correct, and shippable?**

It was built for the **AWS "Agents for Humans" hackathon, Professional Agents track**. Stack: Strands Agents SDK, AWS Bedrock, AWS Bedrock AgentCore, GitHub API, Telegram, and a local file watcher.

### The problem it solves

Developers now delegate real work to AI coding agents. Those agents can:

- touch files they were never meant to touch
- delete or overwrite things silently
- claim "done" when the change does not actually work
- make security-sensitive changes with nobody reviewing them
- drift off-task from what was actually asked

And the review burden falls back on the human. An agent rewrites twelve files in ninety seconds — reading that by hand is slow, and asking the coding agent to summarise its own work is asking the suspect to write the report.

Sentinel sits alongside the coding agent as an **independent, skeptical observer** and delivers a verdict with reasons.

### The verdict

Four levels. These are the spine of the product and should be the spine of the site's visual language:

| Verdict | Meaning |
| --- | --- |
| **SAFE** | Nothing flagged, everything checked, ship it |
| **REVIEW** | A human should look at this before it merges |
| **CONDITIONAL** | Shippable once stated conditions are satisfied |
| **STOP** | Do not ship this as it stands |

### The six features (use these as the Features section)

1. **Action Monitor** — watches every file write, delete, dependency change and git command the coding agent makes. Enforces a per-project path policy (allow-list / deny-list). Real git hooks capture what git was actually told, flags included — so a `git push --force` is caught as it happens.
2. **Code Risk Analyzer** — scores the change: security-sensitive paths, missing tests, unusually large diffs, and how often these files have churned recently.
3. **Reliability Verdict** — combines every signal into one verdict with plain-English reasons. Deterministic: identical signals produce an identical verdict on every run.
4. **Change Explainer** — answers "what did it just do?" from what Sentinel independently observed, not from the coding agent's own account of itself.
5. **Project Norms** — your project's rules in plain English ("no hardcoded model ids", "don't make frontend design calls without asking"). Declared once, checked on every change. Rules a regex can catch are checked deterministically and for free; the rest are judged by a model — but how serious a violation is, is always written by a human.
6. **Natural-Language Control** — ask in words. From a terminal, from a Telegram bot on your phone, or from a web dashboard: "What went wrong?" "Why did you stop it?" "Can we ship?" There is also an MCP server, so the coding agent can consult Sentinel *before* it writes — and every consultation is reported to you.

### Three principles that make Sentinel different — give these real space on the page

These are the most credible thing about the project. Do not bury them in a footer.

- **The LLM never decides the verdict.** The verdict is computed deterministically from the signals. A supervisor that answers SAFE on one run and STOP on the next, for the same input, is not a supervisor. The model narrates what was decided; it never decides.
- **Sentinel never acts.** It reports. Nothing in it reverts, deletes, blocks, or pushes anything. Even the git hooks are built so they can never abort your push.
- **An unverified check is never a pass.** Anything Sentinel could not check is shown as "could not verify" — as prominently as the verdict itself — and caps the result at REVIEW. A clean-looking SAFE while we know we did not look is the worst thing this tool could do.

### Where it runs

One setting chooses the brain, and this is a privacy story worth telling on the page:

- **Local (Ollama)** — a model on your own machine. Nothing leaves it.
- **AWS Bedrock** — with your own AWS credentials.
- **Hosted (Bedrock AgentCore)** — zero setup, no AWS account needed.

License: **Apache 2.0**.

---

## Required content sections

Design the order and rhythm yourself, but all of this must be present:

1. **Hero** — the one-line pitch and the primary download CTA.
2. **A short description of the agent** — what it is, in two or three sentences.
3. **The problem / what it solves** — the bullets above, written as prose, not as a list of buzzwords.
4. **Purpose** — who it is for and why it exists now.
5. **Features** — the six above. Each needs a real explanation, not a four-word card.
6. **The three principles** — treat this as a feature section of its own.
7. **How to install** — see below, this needs care.
8. **Download** — the .exe and .dmg, fetched live (see the hosting section).
9. **Link to the GitHub repo** with the actual code, and the Apache 2.0 license.
10. **A slot for a demo video** (≤5 min, will be provided later — leave a well-designed placeholder that is clearly intentional, not a broken embed).
11. **A slot for an architecture diagram** (also provided later — same treatment).

### The install section needs two paths

- **Installer** — download the .exe (Windows) or .dmg (macOS), install, point it at a repo.
- **From source** — Python 3.11+, clone, `pip install -r requirements.txt`, run the watcher against a repo.

Also include a short, calm note about first-run friction on unsigned builds if applicable — macOS Gatekeeper (right-click → Open) and Windows SmartScreen (More info → Run anyway). Judges will hit this, and pretending it does not exist is worse than explaining it in one line. Leave this section easy to remove if the builds end up fully signed and notarized.

---

## Hosting and the download mechanism — the technical crux

**The site is hosted on GitHub Pages. The installers are NOT hosted on the site.**

They are published as **GitHub Releases on the main code repository**, and the site fetches them at runtime. The whole point: new builds get released on the code repo and the website picks them up with **zero code changes and zero redeploys**.

### How to implement it

Fetch from the GitHub Releases API on page load:

```
GET https://api.github.com/repos/{OWNER}/{REPO}/releases/latest
```

From the response, read `assets[]` and pick by file extension:

- an asset ending in `.exe` → the Windows download
- an asset ending in `.dmg` → the macOS download

Use `browser_download_url` for the link, and surface `name`, `size` (format it human-readably), the release `tag_name` as a version, and `published_at` as a date. `download_count` is a nice touch if it looks good.

### Requirements for this to be robust

- **Detect the visitor's OS** and make the matching installer the primary CTA, with the other clearly available as a secondary option. Never hide one.
- **Handle "no release exists yet" gracefully.** At the time you build this, there will probably be no release at all. The download area must degrade into something intentional and well-designed — "Builds are on the way, star the repo" with a link to the releases page — never a broken button, a spinner that never stops, or a raw error.
- **Handle API failure and rate limiting.** The GitHub API allows 60 unauthenticated requests per hour per IP. Cache the response in `sessionStorage` so a visitor scrolling around does not re-fetch. On any failure, fall back to a plain link to the repo's releases page.
- **Never commit a GitHub token.** The site is public and static. Unauthenticated requests only.
- **Do not hardcode a version number, filename, or download URL anywhere.** Everything comes from the API response.

### GitHub Pages specifics

- Set Vite's `base` correctly for a project page (`/{REPO}/`), or document the change needed if a custom domain is used later.
- Ship a GitHub Actions workflow that builds and deploys to Pages on push to main.
- Prefer a single-page site with anchor navigation. Client-side routing on Pages needs a 404.html hack; avoid needing it.
- Include a `.nojekyll` file.

---

## Placeholders — the repo does not exist publicly yet

The GitHub repository is **not finished and not yet public**. The owner and repo name will be given to you later.

Put **every** unknown in one clearly commented config file (e.g. `src/config.ts`), so filling them in later is a one-file, one-minute change:

```ts
export const config = {
  githubOwner: "PLACEHOLDER_OWNER",
  githubRepo: "PLACEHOLDER_REPO",
  repoUrl: "https://github.com/PLACEHOLDER_OWNER/PLACEHOLDER_REPO",
  demoVideoUrl: "",        // empty = show the designed placeholder
  architectureDiagram: "", // empty = show the designed placeholder
};
```

Do not scatter these strings through components. Do not invent a plausible-looking repo URL and leave it hardcoded somewhere.

---

## Design direction

### The governing idea

**In a supervision tool, colour means status.** Sentinel has a four-rung verdict ladder, and those four are the only saturated colours in the product's own dashboard. Carry that discipline onto the site: anything strongly coloured should mean something. That single rule will do more for the design than any effect.

### Palette (matches the product's real dashboard — keeping them consistent makes the whole thing feel like one considered system)

Built on **warm graphite** neutrals with a faint ochre bias, so the neutral reads as chosen rather than inherited. **No blue**, and no slate or steel greys either — they read as blue-adjacent, and blue is what every generated dev-tool site defaults to.

| Token | Light | Dark |
| --- | --- | --- |
| ground | `#F2F1ED` | `#1A1815` |
| surface | `#FFFFFF` | `#242019` |
| ink / ink-soft | `#191713` / `#5C564C` | `#F0EDE6` / `#A9A192` |
| rule | `#DFDBD2` | `#332D25` |
| **SAFE** | `#2E7D5B` | `#6BC49A` |
| **REVIEW** | `#A6720C` | `#E0A93C` |
| **CONDITIONAL** | `#B4571C` | `#EE9152` |
| **STOP** | `#A83228` | `#F07F72` |
| accent | `#6E4A7E` | `#B98FC9` |

The muted violet is the single non-semantic accent — links, focus rings, interactive affordances only. Never for status. You may extend this palette for the site, but the four verdict colours and the no-blue rule are fixed.

**Type:** IBM Plex Sans for the interface, IBM Plex Mono for paths, code, terminal output and counts. Plex reads as an engineering instrument rather than a marketing page, and it is open-licensed. Deliberately **not Inter** — it is the default every generated site reaches for. If you have a stronger pairing that holds the same character, propose it, but justify it.

**Both light and dark themes**, tokens on `:root`, swapped under `prefers-color-scheme` plus an explicit toggle.

### 3D and motion

I want real 3D and genuinely impressive effects — but **motivated by the product, not decorating it**. Use `react-three-fiber` + `drei`, and Framer Motion for everything 2D.

Ideas that actually mean something (pick, refine, or beat them):

- A hero where files and diffs stream past a watching lens, each getting tagged with its verdict colour in real time — the product's whole behaviour, shown rather than described.
- The four-rung verdict ladder as a physical object you can see a change climb.
- A scroll-driven sequence where an agent's change accumulates risk signals and the verdict resolves at the end.
- Depth and parallax built from real UI — actual terminal output, actual verdict cards — rather than abstract geometry.

**Do not build:** floating gradient blobs, generic particle fields, a spinning 3D logo, a starfield, or a mesh gradient background. Those are the visual signature of a generated site and a judge will clock it instantly.

Rules for motion:

- Lazy-load the 3D canvas. It must never block first paint or tank Lighthouse.
- Provide a static, still-beautiful fallback for mobile and for `prefers-reduced-motion`.
- Nothing may loop distractingly near text someone is trying to read.
- Restraint reads as confidence. A few exceptional moments beat forty animated cards.

---

## The bar: this must not feel vibe-coded

This is the part I care about most. Concrete rules:

**Never:**
- Emoji in headings or body copy
- "🚀 Revolutionary AI-powered next-generation platform" phrasing — or any of it
- Fabricated social proof: fake testimonials, "trusted by" logo walls, invented user counts, fake star counts
- Lorem ipsum, or fake-looking terminal output. If you show output, show the real thing — real verdict text, real file paths, real reasons
- Scroll-jacking, autoplaying sound, cursor-trail gimmicks that fight the scroll
- Every section centred with the same three-card grid underneath
- Claiming the project is more mature than it is. It is a hackathon build. Confident and honest beats inflated

**Always:**
- Plain, declarative sentences. Short. Specific. The product's own voice is dry and precise — match it
- One type scale, consistent vertical rhythm, generous whitespace
- Vary section rhythm: full-bleed, split, offset, dense. Give the eye somewhere to go
- Real content density where it earns attention — judges reward substance
- Every interactive element has a visible hover, focus and active state

---

## Accessibility and performance — non-negotiable

- WCAG AA contrast in both themes. Check the verdict colours specifically; they carry meaning, so they must also carry text contrast, and status must never be conveyed by colour alone — pair it with a label or shape
- Full keyboard navigation, visible focus rings (use the violet accent), semantic landmarks and headings
- `prefers-reduced-motion` respected everywhere, including the 3D
- Lighthouse ≥ 90 on mobile for Performance and 100 for Accessibility. Lazy-load heavy assets, no layout shift
- Responsive from 320px to ultrawide. Test the hero and the 3D at small widths specifically — that is where these sites usually fall apart
- Wide content (code blocks, tables) scrolls inside its own container; the page body never scrolls horizontally

---

## Also include

- A proper favicon and an Open Graph / Twitter card image. Judges will share this link, and the preview is part of the first impression
- Sensible `<title>` and meta description
- A footer with the repo link, the Apache 2.0 license, and the AWS "Agents for Humans" hackathon credit
- A "copy" button on any install command shown

---

## Deliverables

- React + Vite + TypeScript + Tailwind, `react-three-fiber` for 3D, Framer Motion for motion
- A GitHub Actions workflow deploying to GitHub Pages
- A short README: how to run locally, how to build, and **exactly which values in `src/config.ts` to fill in** once the repo is public
- Everything committed and building clean

## Before you call it done

- [ ] With `PLACEHOLDER_OWNER/PLACEHOLDER_REPO` still in place, the site builds, runs, and the download area shows its designed empty state rather than an error
- [ ] Point it at any real public repo with releases and confirm the .exe/.dmg links resolve from the API
- [ ] Kill the network and confirm the download area degrades gracefully
- [ ] Both themes checked, light and dark
- [ ] 320px, tablet, desktop, ultrawide
- [ ] `prefers-reduced-motion` on — the site is still complete and still good
- [ ] Keyboard-only pass through the whole page
- [ ] No hardcoded version, filename, or download URL anywhere in the codebase
- [ ] No placeholder text, no lorem, no emoji in the copy

Ask me anything that is ambiguous before building — I would rather answer three questions now than have the design go sideways.
