/**
 * The supporting panels: status strip, recoveries, consultations, timeline.
 *
 * All quieter than the verdict and the file tree on purpose. They answer
 * follow-up questions, and none of them should compete for the first look.
 */

import type { Consultation, Recovery, SessionInfo, TimelineEvent } from "../types";
import { clock, whenReadable } from "../lib/format";
import "./Panels.css";

export function StatusStrip({
  session,
  repoName,
  live,
  watching,
  onRefresh,
  refreshing,
}: {
  session: SessionInfo;
  repoName: string;
  /** Served by the local server rather than read from a recorded file. */
  live: boolean;
  /**
   * Whether an agent is actually watching right now. Deliberately separate
   * from `live`: a live page with a stopped agent must never say "watching".
   * Claiming to watch when nothing is watching is the one lie a supervisor
   * cannot afford.
   */
  watching?: boolean;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  return (
    <div className="strip">
      <span className="strip-repo">{repoName}</span>

      {session.recorded ? (
        <>
          <span className="strip-item">
            <span className={`pulse${live && watching ? " is-live" : ""}`} aria-hidden="true" />
            {!live ? "recorded session" : watching ? "watching" : "not watching"}
          </span>
          <span className="strip-item">since {whenReadable(session.started ?? "")}</span>
          <span className="strip-item">{session.files_touched ?? 0} file(s) touched</span>
          {(session.skipped?.length ?? 0) > 0 && (
            <span className="strip-item strip-warn">{session.skipped!.length} not snapshotted</span>
          )}
        </>
      ) : (
        <span className="strip-item">no session recorded</span>
      )}

      {live && (
        <button type="button" className="strip-refresh" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing..." : "Refresh"}
        </button>
      )}
    </div>
  );
}

export function Recoveries({ items }: { items: Recovery[] }) {
  if (!items.length) return null;
  return (
    <section className="panel" aria-label="Deleted files that can be restored">
      <h2 className="panel-title">{items.length} deleted file(s) can be restored</h2>
      <p className="panel-note">
        Sentinel kept a copy from before the agent started. It will not run these for you.
      </p>
      <div className="panel-body">
        {items.map((item) => (
          <div key={item.path} className="recovery">
            <code className="recovery-path">{item.path}</code>
            <code className="recovery-command">{item.command}</code>
          </div>
        ))}
      </div>
    </section>
  );
}

export function Consultations({ items }: { items: Consultation[] }) {
  if (!items.length) return null;
  const counts = items.reduce<Record<string, number>>((acc, item) => {
    acc[item.tool] = (acc[item.tool] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <section className="panel" aria-label="What the coding agent asked">
      <h2 className="panel-title">The coding agent consulted Sentinel {items.length} time(s)</h2>
      <p className="panel-note">
        An agent that quietly fixes what it finds is an agent whose mistakes you never see, so every
        question it asked is recorded here.
      </p>
      <div className="chips">
        {Object.entries(counts).map(([tool, count]) => (
          <span key={tool} className="chip">
            {tool} <span className="chip-count">{count}</span>
          </span>
        ))}
      </div>
    </section>
  );
}

const MARKERS: Record<string, string> = { BLOCKED: "STOP", FLAGGED: "FLAG", ALLOWED: "ok" };

export function Timeline({ events }: { events: TimelineEvent[] }) {
  if (!events.length) return null;
  return (
    <section className="panel" aria-label="Timeline">
      <h2 className="panel-title">In order</h2>
      <div className="timeline">
        {events.map((event, index) => (
          <div key={index} className="timeline-row">
            <span className="timeline-clock">{clock(event.at)}</span>
            <span className={`timeline-mark mark-${event.verdict || "none"}`}>
              {MARKERS[event.verdict] ?? ""}
            </span>
            <span className="timeline-what">{event.what}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
