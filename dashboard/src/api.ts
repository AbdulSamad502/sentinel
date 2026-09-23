/**
 * Talking to the local Sentinel server.
 *
 * One app, two modes, decided at build time by `VITE_SESSION_URL`:
 *
 *   * **live** - no `VITE_SESSION_URL`. The Python server is there, so the app
 *     can list agents, set rules and start watching.
 *   * **recorded** - `VITE_SESSION_URL` points at a committed session file.
 *     This is the hosted build a judge clicks into: a real verdict, no setup,
 *     and no control surface at all, because there is no server behind it.
 *
 * `IS_LIVE` gates every control call. A read-only build must never render a
 * button that cannot work.
 *
 * **The token.** Everything that mutates carries a per-session token the server
 * put in the page. That is not authentication - it is what stops another web
 * page you have open from driving this one, because a cross-origin page cannot
 * read our HTML to learn it. See `sentinel/interfaces/guard.py`.
 */

import type {
  Agent,
  Browse,
  Catalog,
  CommandEntry,
  DiscoveredChat,
  Job,
  JobKind,
  ProjectNorms,
  Severity,
  SessionDocument,
  TelegramState,
} from "./types";

const RECORDED_SESSION = import.meta.env.VITE_SESSION_URL ?? "";

/** True when a Python server is behind us and the controls can work. */
export const IS_LIVE = RECORDED_SESSION === "";

const TOKEN_HEADER = "X-Sentinel-Token";

function token(): string {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="sentinel-token"]');
  if (meta?.content) return meta.content;
  // The banner prints a URL carrying the token, so opening that link works even
  // if the page was served from somewhere that could not inject the meta tag.
  return new URLSearchParams(window.location.search).get("token") ?? "";
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function call<T>(route: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(route, {
    method,
    cache: "no-store",
    headers: {
      [TOKEN_HEADER]: token(),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  const text = await response.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(response.status, `The server sent something that was not JSON.`);
  }

  if (!response.ok) {
    const message = (parsed as { error?: string })?.error ?? `Request failed (HTTP ${response.status}).`;
    throw new ApiError(response.status, message);
  }
  return parsed as T;
}

/* --- reading -------------------------------------------------------------- */

export async function fetchSession(agentId?: string): Promise<SessionDocument> {
  if (!IS_LIVE) {
    const response = await fetch(RECORDED_SESSION, { cache: "no-store" });
    if (!response.ok) throw new ApiError(response.status, "Could not load the recorded session.");
    return (await response.json()) as SessionDocument;
  }
  return call<SessionDocument>(`/api/agents/${agentId}/session`);
}

export const fetchAgents = () => call<{ agents: Agent[] }>("/api/agents").then((body) => body.agents);
export const fetchCatalog = () => call<Catalog>("/api/catalog");
export const fetchNorms = (agentId: string) => call<ProjectNorms>(`/api/agents/${agentId}/norms`);
export const fetchBrowse = (path: string) =>
  call<Browse>(`/api/browse?path=${encodeURIComponent(path)}`);
export const fetchCommands = (agentId: string) =>
  call<{ commands: CommandEntry[] }>(`/api/agents/${agentId}/commands`).then((body) => body.commands);
export const fetchLog = (agentId: string) =>
  call<{ lines: string[] }>(`/api/agents/${agentId}/log`).then((body) => body.lines);

/* --- changing ------------------------------------------------------------- */

export const addAgent = (folder: string, name = "") =>
  call<Agent>("/api/agents", "POST", { folder, name });

export const removeAgent = (agentId: string) => call<unknown>(`/api/agents/${agentId}`, "DELETE");

export const startAgent = (agentId: string) => call<Agent>(`/api/agents/${agentId}/start`, "POST", {});
export const stopAgent = (agentId: string) => call<Agent>(`/api/agents/${agentId}/stop`, "POST", {});

export const saveNorms = (agentId: string, selected: string[], custom: string[], severity: Severity) =>
  call<ProjectNorms>(`/api/agents/${agentId}/norms`, "PUT", { selected, custom, severity });

export const startJob = (agentId: string, kind: JobKind) =>
  call<Job>(`/api/agents/${agentId}/jobs`, "POST", { kind });

export const fetchJob = (jobId: string) => call<Job>(`/api/jobs/${jobId}`);

/**
 * Poll a job until it finishes.
 *
 * Narration takes about forty seconds and a full review about a minute and a
 * half, so this is a slow wait by design. `onTick` exists so the interface can
 * keep saying so rather than showing a spinner that reads as a hang.
 */
export async function waitForJob(jobId: string, onTick?: (seconds: number) => void): Promise<Job> {
  const started = Date.now();
  for (;;) {
    const job = await fetchJob(jobId);
    if (job.state !== "running") return job;
    onTick?.(Math.round((Date.now() - started) / 1000));
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

/* --- the Telegram bot ------------------------------------------------------
 *
 * The token is written here and never read back: the server only ever returns
 * its last four characters. Nothing in this file should ever try to display
 * more than `token_hint`.
 */

export const fetchTelegram = () => call<TelegramState>("/api/telegram");

export const saveTelegramToken = (token: string) =>
  call<TelegramState>("/api/telegram", "PUT", { token });

export const saveTelegramChats = (chats: number[]) =>
  call<TelegramState>("/api/telegram", "PUT", { chats });

export const saveTelegramAgent = (agent_id: string) =>
  call<TelegramState>("/api/telegram", "PUT", { agent_id });

export const discoverChats = () =>
  call<{ chats: DiscoveredChat[] }>("/api/telegram/discover", "POST", {}).then((body) => body.chats);

export const sendTelegramTest = () => call<{ sent: number }>("/api/telegram/test", "POST", {});
export const startTelegram = () => call<TelegramState>("/api/telegram/start", "POST", {});
export const stopTelegram = () => call<TelegramState>("/api/telegram/stop", "POST", {});
export const forgetTelegram = () => call<TelegramState>("/api/telegram", "DELETE");
