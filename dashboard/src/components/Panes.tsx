/**
 * The smaller full-pane views: settings, the terminal commands, and the answer
 * to whatever was last asked.
 *
 * They share a file because each is a few dozen lines and splitting them would
 * cost more imports than it saves in clarity.
 */

import { motion } from "framer-motion";
import { useCallback, useEffect, useState } from "react";
import { fetchCommands } from "../api";
import type { CommandEntry, Job } from "../types";
import "./Panes.css";

export type Theme = "system" | "light" | "dark";

/* --- settings ------------------------------------------------------------- */

export function Settings({ theme, onTheme }: { theme: Theme; onTheme: (next: Theme) => void }) {
  return (
    <div className="pane">
      <h2>Settings</h2>

      <section className="pane-block">
        <h3>Theme</h3>
        <p className="panel-note">
          "System" follows whatever this machine is set to.
        </p>
        <div className="view-toggle" role="group" aria-label="Colour theme">
          {(["system", "light", "dark"] as Theme[]).map((option) => (
            <button
              key={option}
              type="button"
              className={`toggle-button${theme === option ? " is-on" : ""}`}
              aria-pressed={theme === option}
              onClick={() => onTheme(option)}
            >
              {option}
            </button>
          ))}
        </div>
      </section>

      <section className="pane-block">
        <h3>What colour means here</h3>
        <p className="panel-note">
          Colour means status and nothing else. The four verdict levels are the only saturated
          colours in the interface, so anything coloured is something that wants your attention.
        </p>
        <ul className="legend">
          <li><span className="legend-dot legend-safe" /> SAFE - nothing flagged, everything checked</li>
          <li><span className="legend-dot legend-review" /> REVIEW - a human should look before it merges</li>
          <li><span className="legend-dot legend-conditional" /> CONDITIONAL - shippable once conditions are met</li>
          <li><span className="legend-dot legend-stop" /> STOP - do not ship this as it stands</li>
        </ul>
      </section>
    </div>
  );
}

/* --- terminal commands ---------------------------------------------------- */

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard access can be refused; the command is on screen either way.
    }
  }, [text]);

  return (
    <button type="button" className="copy-button" onClick={() => void copy()}>
      {copied ? "copied" : "copy"}
    </button>
  );
}

export function CommandsPane({ agentId, agentName }: { agentId: string; agentName: string }) {
  const [commands, setCommands] = useState<CommandEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const loaded = await fetchCommands(agentId);
        if (!cancelled) setCommands(loaded);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  return (
    <div className="pane">
      <h2>The same thing, from a terminal</h2>
      <p className="panel-note">
        Every button in this interface runs one of these. If you would rather type, nothing here
        needs the dashboard at all - these are the commands for {agentName}.
      </p>
      {error ? <p className="picker-error">{error}</p> : null}

      <ul className="command-list">
        {commands.map((entry) => (
          <li key={entry.command}>
            <span className="command-label">{entry.label}</span>
            <div className="command-row">
              <code>{entry.command}</code>
              <CopyButton text={entry.command} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* --- the answer to the last question -------------------------------------- */

const WAITING = {
  explain: "Reading the diff and describing what the agent did...",
  review: "Gathering every signal and deciding whether this is shippable...",
  why: "Working out what went wrong...",
  rules: "Checking the change against this project's rules...",
  fix: "Composing a message you can send the coding agent...",
  doctor: "Checking this project's setup...",
} as const;

export function JobPane({ job, seconds, onClose }: { job: Job; seconds: number; onClose: () => void }) {
  return (
    <motion.section
      className="job-pane"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.16, ease: "easeOut" }}
    >
      <header className="job-head">
        <h3>{WAITING[job.kind] && job.state === "running" ? "Working" : "Answer"}</h3>
        <button type="button" className="link-button" onClick={onClose}>
          Close
        </button>
      </header>

      {job.state === "running" ? (
        <div className="job-waiting">
          <motion.span
            className="job-bar"
            animate={{ opacity: [0.35, 1, 0.35] }}
            transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
          />
          <p>{WAITING[job.kind]}</p>
          {/* Say how long, rather than showing a spinner that reads as a hang. */}
          <p className="panel-note">
            {seconds}s so far. Anything that needs the model usually takes about a minute.
          </p>
        </div>
      ) : job.state === "failed" ? (
        <p className="picker-error">{job.error}</p>
      ) : (
        <pre className="job-output">{job.output}</pre>
      )}
    </motion.section>
  );
}
