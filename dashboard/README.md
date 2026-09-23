# Sentinel dashboard

React + Vite + TypeScript. One app, two data sources.

```bash
npm install
npm run dev        # talks to the local Python server on :8765
npm test           # tree logic, via Node's own runner - no test dependency
npm run build
```

## The two modes

The app fetches a session document from `VITE_SESSION_URL`. Nothing else in the
app knows the difference.

**Live** - unset, so it reads `/session.json` from the Python server:

```bash
python -m sentinel.action_monitor.watcher <repo>     # in one terminal
python -m sentinel.interfaces.dashboard <repo>       # in another
npm run build && open http://127.0.0.1:8765
```

**Hosted** - a recorded session committed as a static file, so a visitor sees a
real verdict with no setup at all:

```bash
python -m sentinel.interfaces.export <repo> -o public/sample-session.json
VITE_SESSION_URL=./sample-session.json npm run build
```

`dist/` is then a plain static site. On Cloudflare Pages: build command
`npm install && VITE_SESSION_URL=./sample-session.json npm run build`, output
directory `dist`. **No backend** - the real one ships with the desktop app.

## Design

Colour means status, and nothing else. Sentinel has a four-rung verdict ladder,
so those four are the only saturated colours in the interface; anything coloured
is something that needs attention. The neutrals are warm graphite - no blue, and
no slate greys either, since those read blue-adjacent. A muted violet is the one
non-semantic accent, reserved for interactive affordances, so "you can click
this" never gets confused with "this is a problem".

The file navigator is one implementation for every platform. Rather than
imitating Finder or Explorer, it uses what both share and everyone knows:
disclosure chevrons, folders before files, indent guides, and arrow keys where
Right opens a folder and Left closes it or jumps to the parent. Folders inherit
the worst severity inside them, so nothing hides behind a collapsed chevron, and
a folder holding a single folder folds into one row.

`Could not verify` is given the same weight as the verdict itself. The whole
tool rests on an unverified check never reading as a pass, and an interface that
tucked it into a footnote would be quietly contradicting that.
