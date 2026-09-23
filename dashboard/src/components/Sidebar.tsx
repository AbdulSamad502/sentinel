/**
 * The list of supervised repos, and everything you can do to one.
 *
 * A person can have several agents - one per project - so this is where you
 * move between them. Colour follows the rule the whole interface obeys: the
 * status dot is the only coloured thing in here, because it is the only thing
 * that carries status.
 */

import { motion } from "framer-motion";
import type { Agent, JobKind } from "../types";
import "./Sidebar.css";

export type View = "session" | "norms" | "telegram" | "commands" | "settings";

/** The feature buttons, and the question each one really asks. */
const FEATURES: { kind: JobKind; label: string; slow: boolean }[] = [
  { kind: "explain", label: "What did it do?", slow: true },
  { kind: "review", label: "Can we ship?", slow: true },
  { kind: "why", label: "What went wrong?", slow: true },
  { kind: "rules", label: "Did it follow the rules?", slow: true },
  { kind: "fix", label: "Message for the agent", slow: false },
  { kind: "doctor", label: "Is this set up right?", slow: false },
];

interface Props {
  agents: Agent[];
  selectedId: string | null;
  view: View;
  visualize: boolean;
  busy: boolean;
  collapsed: boolean;
  onSelect: (id: string) => void;
  onView: (view: View) => void;
  onVisualize: (on: boolean) => void;
  onCollapse: (collapsed: boolean) => void;
  onAdd: () => void;
  onStart: () => void;
  onStop: () => void;
  onRemove: () => void;
  onFeature: (kind: JobKind) => void;
}

function StatusDot({ agent }: { agent: Agent }) {
  const { state, detail } = agent.status;
  const title = detail || (state === "running" ? "watching" : "not watching");

  return (
    <span className={`dot dot-${state}`} title={title} aria-label={title}>
      {state === "running" ? (
        <motion.span
          className="dot-pulse"
          animate={{ opacity: [0.9, 0.15, 0.9], scale: [1, 1.9, 1] }}
          transition={{ duration: 2.4, repeat: Infinity, ease: "easeInOut" }}
        />
      ) : null}
    </span>
  );
}

export function Sidebar(props: Props) {
  const { agents, selectedId, view, visualize, busy, collapsed } = props;
  const selected = agents.find((agent) => agent.id === selectedId) ?? null;
  const running = selected?.status.state === "running" || selected?.status.state === "unverified";

  // Collapsed, it becomes a rail rather than vanishing outright. A panel with
  // no visible way back is a panel people lose.
  if (collapsed) {
    return (
      <nav className="sidebar sidebar-rail" aria-label="Your agents">
        <button
          type="button"
          className="rail-button"
          aria-label="Show the sidebar"
          aria-expanded={false}
          title="Show the sidebar"
          onClick={() => props.onCollapse(false)}
        >
          &rsaquo;
        </button>
        {agents.map((agent) => (
          <button
            key={agent.id}
            type="button"
            className={`rail-agent${agent.id === selectedId ? " is-on" : ""}`}
            title={agent.name}
            aria-label={agent.name}
            onClick={() => {
              props.onSelect(agent.id);
              props.onCollapse(false);
            }}
          >
            <StatusDot agent={agent} />
          </button>
        ))}
      </nav>
    );
  }

  return (
    <nav className="sidebar" aria-label="Your agents">
      <div className="sidebar-brand">
        <div className="brand-row">
          <h1>Sentinel</h1>
          <button
            type="button"
            className="rail-button"
            aria-label="Hide the sidebar"
            aria-expanded
            title="Hide the sidebar"
            onClick={() => props.onCollapse(true)}
          >
            &lsaquo;
          </button>
        </div>
        <p>Supervising your coding agents.</p>
      </div>

      <div className="sidebar-section">
        <div className="sidebar-heading">
          <span>Repos</span>
          <button type="button" className="link-button" onClick={props.onAdd}>
            + Add
          </button>
        </div>

        {agents.length === 0 ? (
          <p className="sidebar-empty">
            No repos yet. Add the folder your coding agent is about to work in.
          </p>
        ) : (
          <ul className="agent-list">
            {agents.map((agent) => (
              <li key={agent.id}>
                <button
                  type="button"
                  className={`agent-button${agent.id === selectedId ? " is-on" : ""}`}
                  aria-current={agent.id === selectedId}
                  onClick={() => props.onSelect(agent.id)}
                >
                  <StatusDot agent={agent} />
                  <span className="agent-name">{agent.name}</span>
                  {!agent.exists ? <span className="agent-warn">missing</span> : null}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {selected ? (
        <>
          <div className="sidebar-section">
            <div className="sidebar-heading">
              <span>{selected.name}</span>
            </div>
            <p className="agent-path" title={selected.root}>
              {selected.root}
            </p>

            <button
              type="button"
              className={`primary-button${running ? " is-stop" : ""}`}
              disabled={busy || !selected.exists}
              onClick={running ? props.onStop : props.onStart}
            >
              {busy ? "Working..." : running ? "Stop watching" : "Start watching"}
            </button>

            {selected.status.detail ? (
              <p className="agent-detail">{selected.status.detail}</p>
            ) : null}

            <div className="sidebar-actions">
              <button
                type="button"
                className={`side-button${view === "session" ? " is-on" : ""}`}
                onClick={() => props.onView("session")}
              >
                Changes
              </button>
              <button
                type="button"
                className={`side-button${view === "norms" ? " is-on" : ""}`}
                onClick={() => props.onView("norms")}
              >
                Edit rules
              </button>
            </div>

            {view === "session" ? (
              <div className="view-toggle" role="group" aria-label="How to show the changes">
                <button
                  type="button"
                  className={`toggle-button${!visualize ? " is-on" : ""}`}
                  aria-pressed={!visualize}
                  onClick={() => props.onVisualize(false)}
                >
                  Files
                </button>
                <button
                  type="button"
                  className={`toggle-button${visualize ? " is-on" : ""}`}
                  aria-pressed={visualize}
                  onClick={() => props.onVisualize(true)}
                >
                  Visualize
                </button>
              </div>
            ) : null}
          </div>

          <div className="sidebar-section">
            <div className="sidebar-heading">
              <span>Ask</span>
            </div>
            <ul className="feature-list">
              {FEATURES.map((feature) => (
                <li key={feature.kind}>
                  <button
                    type="button"
                    className="feature-button"
                    disabled={busy}
                    onClick={() => props.onFeature(feature.kind)}
                  >
                    {feature.label}
                    {feature.slow ? <span className="feature-slow" title="Needs the model; takes about a minute">~1m</span> : null}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </>
      ) : null}

      <div className="sidebar-foot">
        <button
          type="button"
          className={`side-button${view === "telegram" ? " is-on" : ""}`}
          onClick={() => props.onView("telegram")}
        >
          Telegram bot
        </button>
        <button
          type="button"
          className={`side-button${view === "commands" ? " is-on" : ""}`}
          onClick={() => props.onView("commands")}
          disabled={!selected}
        >
          Terminal commands
        </button>
        <button
          type="button"
          className={`side-button${view === "settings" ? " is-on" : ""}`}
          onClick={() => props.onView("settings")}
        >
          Settings
        </button>
        {selected ? (
          <button type="button" className="danger-link" onClick={props.onRemove}>
            Remove {selected.name}
          </button>
        ) : null}
        <p className="sidebar-note">
          Sentinel observes and reports. It never changes your code.
        </p>
      </div>
    </nav>
  );
}
