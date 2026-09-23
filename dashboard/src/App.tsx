/**
 * Sentinel's dashboard.
 *
 * This is where you point Sentinel at a repo, say what its rules are, and
 * start it. The terminal is the optional path now, not the required one - which
 * is the whole reason this exists: a person should not have to learn three
 * commands before the interface shows them anything.
 *
 * **It still never decides anything.** Every verdict here came from
 * `synthesize()`, every answer from `query.answer()`. The page reads and asks;
 * it does not judge.
 *
 * Two modes, decided at build time (see `api.ts`): live against the local
 * server, or read-only over a recorded session. The read-only build is the one
 * a judge clicks into, so it must never render a control that cannot work.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  IS_LIVE,
  fetchAgents,
  fetchSession,
  removeAgent,
  startAgent,
  startJob,
  stopAgent,
  waitForJob,
} from "./api";
import type { Agent, Job, JobKind, SessionDocument } from "./types";
import { allFolders, buildTree, firstFile } from "./lib/tree";
import { FileTree } from "./components/FileTree";
import { DiffView } from "./components/DiffView";
import { SpaceView } from "./components/SpaceView";
import { VerdictHero } from "./components/VerdictHero";
import { Consultations, Recoveries, StatusStrip, Timeline } from "./components/Panels";
import { Sidebar, type View } from "./components/Sidebar";
import { FolderPicker } from "./components/FolderPicker";
import { NormsEditor } from "./components/NormsEditor";
import { Telegram } from "./components/Telegram";
import { CommandsPane, JobPane, Settings, type Theme } from "./components/Panes";
import "./App.css";

/** How often to re-read a live session, so the page keeps up with the agent. */
const POLL_MS = 4000;

function useTheme(): [Theme, (next: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("sentinel-theme") as Theme) ?? "system",
  );

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    localStorage.setItem("sentinel-theme", theme);
  }, [theme]);

  return [theme, setTheme];
}

export default function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [doc, setDoc] = useState<SessionDocument | null>(null);
  const [view, setView] = useState<View>("session");
  const [visualize, setVisualize] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sentinel-sidebar") === "collapsed",
  );
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [jobSeconds, setJobSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [theme, setTheme] = useTheme();
  const main = useRef<HTMLElement | null>(null);

  useEffect(() => {
    localStorage.setItem("sentinel-sidebar", collapsed ? "collapsed" : "open");
  }, [collapsed]);

  const selected = useMemo(
    () => agents.find((agent) => agent.id === selectedId) ?? null,
    [agents, selectedId],
  );

  /* --- loading ----------------------------------------------------------- */

  const loadAgents = useCallback(async () => {
    if (!IS_LIVE) return;
    try {
      const listed = await fetchAgents();
      setAgents(listed);
      setError(null);
      setSelectedId((current) => current ?? listed[0]?.id ?? null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  const loadSession = useCallback(
    async (agentId: string | null, firstTime: boolean) => {
      if (IS_LIVE && !agentId) {
        setDoc(null);
        setLoading(false);
        return;
      }
      try {
        const next = await fetchSession(agentId ?? undefined);
        setDoc(next);
        setError(null);
        if (firstTime) {
          const tree = buildTree(next.changes, next.findings);
          setOpen(new Set(allFolders(tree)));
          setSelectedFile(firstFile(tree)?.path ?? null);
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void (async () => {
      await loadAgents();
      if (!IS_LIVE) await loadSession(null, true);
    })();
  }, [loadAgents, loadSession]);

  useEffect(() => {
    if (!IS_LIVE || !selectedId) return;
    void loadSession(selectedId, true);
  }, [selectedId, loadSession]);

  // Keep a live page current while an agent is watching. Cheap: the document is
  // rebuilt from disk with no model involved.
  useEffect(() => {
    if (!IS_LIVE) return;
    const timer = setInterval(() => {
      void loadAgents();
      if (selectedId && view === "session") void loadSession(selectedId, false);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [loadAgents, loadSession, selectedId, view]);

  /* --- actions ----------------------------------------------------------- */

  const act = useCallback(
    async (work: () => Promise<unknown>) => {
      setBusy(true);
      try {
        await work();
        await loadAgents();
        setError(null);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setBusy(false);
      }
    },
    [loadAgents],
  );

  const runFeature = useCallback(
    async (kind: JobKind) => {
      if (!selectedId) return;
      setJobSeconds(0);

      // Take the reader to the top, where the answer appears. Without this the
      // page looks like nothing happened - the panel opens above whatever they
      // were reading, and a model-backed question takes about a minute to come
      // back. The scroll is the assurance that it is coming.
      setView("session");
      main.current?.scrollTo({
        top: 0,
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      });

      try {
        const started = await startJob(selectedId, kind);
        setJob(started);
        const finished = await waitForJob(started.id, setJobSeconds);
        setJob(finished);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
        setJob(null);
      }
    },
    [selectedId],
  );

  const tree = useMemo(() => buildTree(doc?.changes ?? [], doc?.findings ?? []), [doc]);
  const change = useMemo(
    () => doc?.changes.find((item) => item.path === selectedFile) ?? null,
    [doc, selectedFile],
  );
  const findingsHere = useMemo(
    () => (doc?.findings ?? []).filter((finding) => finding.path === selectedFile),
    [doc, selectedFile],
  );

  const toggle = useCallback((path: string) => {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  /* --- the recorded, read-only build -------------------------------------- */

  if (!IS_LIVE) {
    return (
      <div className="shell shell-recorded">
        <header className="masthead">
          <div className="masthead-title">
            <h1>Sentinel</h1>
            <p>What your coding agent did, and whether it is shippable.</p>
          </div>
          <div className="theme-toggle" role="group" aria-label="Colour theme">
            {(["system", "light", "dark"] as Theme[]).map((option) => (
              <button
                key={option}
                type="button"
                className={`theme-button${theme === option ? " is-on" : ""}`}
                aria-pressed={theme === option}
                onClick={() => setTheme(option)}
              >
                {option}
              </button>
            ))}
          </div>
        </header>

        <p className="recorded-banner">
          A real recorded session, shown read-only. Run Sentinel locally to watch your own repo.
        </p>

        {doc ? (
          <SessionView
            doc={doc}
            tree={tree}
            open={open}
            selectedFile={selectedFile}
            change={change}
            findingsHere={findingsHere}
            visualize={visualize}
            onVisualize={setVisualize}
            onToggle={toggle}
            onSelectFile={setSelectedFile}
            onSetOpen={setOpen}
            loading={loading}
            onRefresh={() => void loadSession(null, false)}
          />
        ) : (
          <p className="panel-note">{error ?? "Loading the session..."}</p>
        )}
      </div>
    );
  }

  /* --- the live app ------------------------------------------------------- */

  return (
    <div className={`app${collapsed ? " is-collapsed" : ""}`}>
      <Sidebar
        agents={agents}
        selectedId={selectedId}
        view={view}
        visualize={visualize}
        busy={busy}
        collapsed={collapsed}
        onCollapse={setCollapsed}
        onSelect={(id) => {
          setSelectedId(id);
          setView("session");
          setJob(null);
        }}
        onView={setView}
        onVisualize={setVisualize}
        onAdd={() => setPicking(true)}
        onStart={() => void act(() => startAgent(selectedId!))}
        onStop={() => void act(() => stopAgent(selectedId!))}
        onRemove={() => {
          if (!selected) return;
          if (!window.confirm(`Stop supervising ${selected.name}? Nothing inside the folder is touched.`)) return;
          void act(async () => {
            await removeAgent(selected.id);
            setSelectedId(null);
            setDoc(null);
          });
        }}
        onFeature={(kind) => void runFeature(kind)}
      />

      <main className="main" ref={main}>
        {error ? <p className="app-error">{error}</p> : null}

        {/* App-level panes first: settings and the bot belong to the install,
            not to a repo, so they must not be gated behind picking one. */}
        {view === "settings" ? (
          <Settings theme={theme} onTheme={setTheme} />
        ) : view === "telegram" ? (
          <Telegram agents={agents} />
        ) : !selected ? (
          <section className="welcome">
            <h2>Point Sentinel at a folder.</h2>
            <p>
              Add the repo your coding agent is about to work in, say what the rules are, and press
              start. Sentinel watches from the outside and tells you what happened.
            </p>
            <button type="button" className="primary-button" onClick={() => setPicking(true)}>
              Add a repo
            </button>
          </section>
        ) : view === "norms" ? (
          <NormsEditor
            agentId={selected.id}
            agentName={selected.name}
            onSaved={() => void loadAgents()}
          />
        ) : view === "commands" ? (
          <CommandsPane agentId={selected.id} agentName={selected.name} />
        ) : doc ? (
          <>
            {job ? (
              <JobPane job={job} seconds={jobSeconds} onClose={() => setJob(null)} />
            ) : null}
            <SessionView
              doc={doc}
              watching={selected.status.state !== "stopped"}
              tree={tree}
              open={open}
              selectedFile={selectedFile}
              change={change}
              findingsHere={findingsHere}
              visualize={visualize}
              onVisualize={setVisualize}
              onToggle={toggle}
              onSelectFile={setSelectedFile}
              onSetOpen={setOpen}
              loading={loading}
              onRefresh={() => void loadSession(selectedId, false)}
            />
          </>
        ) : (
          <p className="panel-note">Loading...</p>
        )}
      </main>

      {picking ? (
        <FolderPicker
          onClose={() => setPicking(false)}
          onAdded={(agent) => {
            setPicking(false);
            setSelectedId(agent.id);
            // A fresh repo has no rules yet, so go straight to the step that
            // matters rather than showing an empty session.
            setView("norms");
            void loadAgents();
          }}
        />
      ) : null}
    </div>
  );
}

/* --- the session itself, shared by both modes ----------------------------- */

interface SessionViewProps {
  doc: SessionDocument;
  tree: ReturnType<typeof buildTree>;
  open: Set<string>;
  selectedFile: string | null;
  change: ReturnType<typeof Object> | null;
  findingsHere: SessionDocument["findings"];
  visualize: boolean;
  loading: boolean;
  watching?: boolean;
  onVisualize: (on: boolean) => void;
  onToggle: (path: string) => void;
  onSelectFile: (path: string) => void;
  onSetOpen: (next: Set<string>) => void;
  onRefresh: () => void;
}

function SessionView(props: SessionViewProps) {
  const { doc } = props;

  return (
    <>
      <StatusStrip
        session={doc.session}
        repoName={doc.repo.name}
        live={IS_LIVE}
        watching={props.watching}
        refreshing={props.loading}
        onRefresh={props.onRefresh}
      />

      {doc.verdict ? (
        <VerdictHero verdict={doc.verdict} />
      ) : (
        <section className="panel">
          <h2 className="panel-title">Nothing recorded yet</h2>
          <p className="panel-note">
            Press "Start watching", then let your coding agent work. This fills in as it does.
          </p>
        </section>
      )}

      {props.visualize ? (
        <SpaceView
          changes={doc.changes}
          findings={doc.findings}
          verdict={doc.verdict}
          repoName={doc.repo.name}
          selected={props.selectedFile}
          onSelect={props.onSelectFile}
        />
      ) : null}

      <section className="workspace" aria-label="Changed files">
        <aside className="workspace-tree">
          <FileTree
            tree={props.tree}
            open={props.open}
            selected={props.selectedFile}
            onToggle={props.onToggle}
            onSelect={props.onSelectFile}
            onSetOpen={props.onSetOpen}
          />
        </aside>
        <div className="workspace-detail">
          <DiffView change={props.change as never} findings={props.findingsHere} />
        </div>
      </section>

      <Recoveries items={doc.recoveries} />
      <Consultations items={doc.consultations} />
      <Timeline events={doc.timeline} />

      <footer className="footer">
        Sentinel observes, analyses and reports. It does not change your code.
      </footer>
    </>
  );
}
