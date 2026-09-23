/**
 * The shape of `session.json`, mirroring `sentinel/interfaces/export.py`.
 *
 * Keys stay snake_case: they come straight from Python and translating cases at
 * the boundary is one more thing to get wrong for no gain.
 */

export type VerdictLevel = "SAFE" | "REVIEW" | "CONDITIONAL" | "STOP";
export type ChangeStatus = "added" | "modified" | "deleted";
export type Severity = "ALLOWED" | "FLAGGED" | "BLOCKED";
/** A regex hit is a fact; a model's judgement is an opinion. Never render them alike. */
export type FindingSource = "pattern" | "model";

export interface FileChange {
  path: string;
  status: ChangeStatus;
  added: number;
  removed: number;
  diff: string;
  diff_truncated: boolean;
}

export interface Finding {
  norm_id: string;
  statement: string;
  path: string;
  line: number;
  severity: Severity;
  source: FindingSource;
  evidence: string;
}

export interface Verdict {
  level: VerdictLevel;
  headline: string;
  reasons: string[];
  conditions: string[];
  unchecked: string[];
}

export interface Recovery {
  path: string;
  command: string;
}

export interface Consultation {
  at: string;
  tool: string;
  detail?: string;
}

export interface TimelineEvent {
  at: string;
  kind: "file" | "git" | "consultation";
  what: string;
  verdict: string;
  reasons: string[];
}

export interface SessionInfo {
  recorded: boolean;
  started?: string;
  files_touched?: number;
  skipped?: { path: string; why: string }[];
}

export interface SessionDocument {
  generated: string;
  repo: { root: string; name: string };
  session: SessionInfo;
  verdict: Verdict | null;
  changes: FileChange[];
  findings: Finding[];
  recoveries: Recovery[];
  consultations: Consultation[];
  timeline: TimelineEvent[];
}

/* --- the control plane ---------------------------------------------------
 *
 * Everything below exists only when the dashboard is talking to the local
 * Python server. The hosted build reads a recorded session and never sees any
 * of it, which is why `IS_LIVE` gates every use.
 */

export type AgentState = "running" | "stopped" | "unverified";

export interface AgentStatus {
  state: AgentState;
  pid: number;
  /** Why we are not certain, when we are not. Always shown rather than hidden. */
  detail: string;
}

export interface Agent {
  id: string;
  name: string;
  root: string;
  added: string;
  pid: number;
  status: AgentStatus;
  exists: boolean;
}

/** How a rule is checked. A regex hit is a fact; a model's judgement is an opinion. */
export type CheckedBy = "pattern" | "model";

export interface CatalogNorm {
  id: string;
  statement: string;
  severity: Severity;
  checked_by: CheckedBy;
}

export interface CatalogGroup {
  title: string;
  norms: CatalogNorm[];
}

export interface Preset {
  key: string;
  label: string;
  norm_ids: string[];
}

export interface Catalog {
  presets: Preset[];
  groups: CatalogGroup[];
}

export interface ProjectNorms {
  configured: boolean;
  severity: Severity;
  selected: string[];
  custom: string[];
}

export interface FolderEntry {
  name: string;
  path: string;
  /** Already has a sentinel.config.json, so somebody set this up before. */
  configured: boolean;
  is_git: boolean;
}

export interface Browse {
  path: string;
  parent: string;
  home: string;
  entries: FolderEntry[];
}

export type JobKind = "explain" | "review" | "why" | "rules" | "fix" | "doctor";
export type JobState = "running" | "done" | "failed";

export interface Job {
  id: string;
  kind: JobKind;
  agent_id: string;
  state: JobState;
  output: string;
  error: string;
  started: string;
  finished: string;
}

export interface CommandEntry {
  label: string;
  command: string;
}

/** The Telegram bot's state, as the page is allowed to see it. */
export interface TelegramState {
  configured: boolean;
  /** The last four characters only. The real token never leaves the server. */
  token_hint: string;
  chats: number[];
  agent_id: string;
  agent_name: string;
  status: AgentStatus;
  /** Only present in the reply to saving a token. */
  username?: string;
}

export interface DiscoveredChat {
  id: number;
  name: string;
  approved: boolean;
}
