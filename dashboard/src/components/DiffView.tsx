/**
 * One file's diff, with a breadcrumb showing where it sits in the repo.
 *
 * The diff is rendered line by line rather than as a code block so added and
 * removed lines can carry their own colour - which here means status, like
 * everywhere else in the interface.
 */

import { useMemo } from "react";
import type { FileChange, Finding } from "../types";
import { basename, parentSegments, severityVar, statusMark, statusVar } from "../lib/format";
import "./DiffView.css";

interface Props {
  change: FileChange | null;
  findings: Finding[];
}

type LineKind = "add" | "remove" | "meta" | "hunk" | "context";

function classify(line: string): LineKind {
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

export function DiffView({ change, findings }: Props) {
  const lines = useMemo(() => (change ? change.diff.split("\n") : []), [change]);

  if (!change) {
    return (
      <div className="diff-empty">
        <p>Select a file to see what changed in it.</p>
      </div>
    );
  }

  const folders = parentSegments(change.path);

  return (
    <div className="diff">
      <header className="diff-head">
        <nav className="crumbs" aria-label="File location">
          {folders.map((segment, index) => (
            <span key={`${segment}-${index}`} className="crumb">
              {segment}
              <span className="crumb-sep" aria-hidden="true">
                /
              </span>
            </span>
          ))}
          <span className="crumb crumb-file">{basename(change.path)}</span>
        </nav>

        <div className="diff-meta">
          <span className="diff-status" style={{ color: statusVar[change.status] }}>
            {statusMark[change.status]} {change.status}
          </span>
          {change.added > 0 && <span className="diff-add">+{change.added}</span>}
          {change.removed > 0 && <span className="diff-remove">-{change.removed}</span>}
        </div>
      </header>

      {findings.length > 0 && (
        <div className="diff-findings">
          {findings.map((finding, index) => (
            <div key={`${finding.norm_id}-${index}`} className="diff-finding">
              <span className="finding-dot" style={{ background: severityVar[finding.severity] }} aria-hidden="true" />
              <div className="finding-body">
                <p className="finding-statement">{finding.statement}</p>
                <p className="finding-detail">
                  <code>{finding.norm_id}</code>
                  {finding.line > 0 && <span> line {finding.line}</span>}
                  <span className={`finding-source finding-source-${finding.source}`}>
                    found by {finding.source}
                  </span>
                </p>
                <code className="finding-evidence">{finding.evidence}</code>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="diff-body">
        {change.diff ? (
          <pre className="diff-pre">
            {lines.map((line, index) => {
              const kind = classify(line);
              return (
                <span key={index} className={`diff-line diff-${kind}`}>
                  {line || " "}
                </span>
              );
            })}
          </pre>
        ) : (
          <p className="diff-none">No textual diff for this file.</p>
        )}
        {change.diff_truncated && <p className="diff-truncated">This diff was shortened for display.</p>}
      </div>
    </div>
  );
}
