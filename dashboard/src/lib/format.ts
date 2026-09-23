import type { ChangeStatus, Severity, VerdictLevel } from "../types";

/** Semantic colour lookups. Every one of these is a status, never decoration. */
export const verdictVar: Record<VerdictLevel, string> = {
  SAFE: "var(--safe)",
  REVIEW: "var(--review)",
  CONDITIONAL: "var(--conditional)",
  STOP: "var(--stop)",
};

export const verdictWash: Record<VerdictLevel, string> = {
  SAFE: "var(--safe-wash)",
  REVIEW: "var(--review-wash)",
  CONDITIONAL: "var(--conditional-wash)",
  STOP: "var(--stop-wash)",
};

export const severityVar: Record<Severity, string> = {
  ALLOWED: "var(--ink-faint)",
  FLAGGED: "var(--review)",
  BLOCKED: "var(--stop)",
};

/** One character, so a tree row stays scannable at a glance. */
export const statusMark: Record<ChangeStatus, string> = {
  added: "A",
  modified: "M",
  deleted: "D",
};

export const statusVar: Record<ChangeStatus, string> = {
  added: "var(--add)",
  modified: "var(--review)",
  deleted: "var(--remove)",
};

export function clock(iso: string): string {
  return iso.length >= 19 ? iso.slice(11, 19) : iso;
}

export function whenReadable(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** The file's own name, for a breadcrumb's last crumb. */
export function basename(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function parentSegments(path: string): string[] {
  return path.split("/").filter(Boolean).slice(0, -1);
}
