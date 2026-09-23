/**
 * Setting up the Telegram bot, with nothing typed in a terminal.
 *
 * The bot is the one feature whose whole point is *not being at your computer*,
 * so making people export two environment variables to reach it was backwards.
 * Five numbered steps, each unlocking the next.
 *
 * Two things this interface must never soften:
 *
 *   * **The token is a secret.** It is written once and never read back - the
 *     server only ever returns its last four characters, so there is nothing
 *     here that could display it even by accident.
 *   * **The allow-list is a security boundary.** Answers quote real source out
 *     of a private repo, and anyone can find a bot by name. Discovering a chat
 *     is not the same as letting it in: every id has to be approved by hand.
 */

import { useCallback, useEffect, useState } from "react";
import {
  discoverChats,
  fetchTelegram,
  forgetTelegram,
  saveTelegramAgent,
  saveTelegramChats,
  saveTelegramToken,
  sendTelegramTest,
  startTelegram,
  stopTelegram,
} from "../api";
import type { Agent, DiscoveredChat, TelegramState } from "../types";
import "./Telegram.css";

interface Props {
  agents: Agent[];
}

function Step({ n, title, done, children }: { n: number; title: string; done?: boolean; children: React.ReactNode }) {
  return (
    <section className={`tg-step${done ? " is-done" : ""}`}>
      <div className="tg-step-head">
        <span className="tg-step-number">{done ? "✓" : n}</span>
        <h3>{title}</h3>
      </div>
      <div className="tg-step-body">{children}</div>
    </section>
  );
}

export function Telegram({ agents }: Props) {
  const [state, setState] = useState<TelegramState | null>(null);
  const [token, setToken] = useState("");
  const [found, setFound] = useState<DiscoveredChat[] | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await fetchTelegram());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = useCallback(
    async (what: string, work: () => Promise<unknown>) => {
      setBusy(what);
      setError(null);
      setNote(null);
      try {
        await work();
        await load();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setBusy("");
      }
    },
    [load],
  );

  if (!state) {
    return <div className="pane"><p className="panel-note">{error ?? "Loading..."}</p></div>;
  }

  const running = state.status.state !== "stopped";
  const paired = state.chats.length > 0;

  return (
    <div className="pane tg">
      <div>
        <h2>Ask Sentinel from your phone</h2>
        <p className="panel-note">
          Hand a coding agent a task, walk away, and still find out what it did and whether it is
          shippable. You can start and stop watching from Telegram too, so a session you kick off
          away from your desk is still supervised.
        </p>
      </div>

      {error ? <p className="picker-error">{error}</p> : null}
      {note ? <p className="tg-note">{note}</p> : null}

      <Step n={1} title="Create a bot" done={state.configured}>
        <p className="panel-note">
          In Telegram, open <strong>@BotFather</strong> and send <code>/newbot</code>. It asks for a
          name, then a username ending in <code>bot</code>. It replies with a token that looks like{" "}
          <code>123456789:AAE...</code>
        </p>
        <a className="link-button tg-link" href="https://t.me/BotFather" target="_blank" rel="noreferrer">
          Open @BotFather
        </a>
      </Step>

      <Step n={2} title="Paste the token here" done={state.configured}>
        {state.configured ? (
          <p className="tg-settled">
            Token saved (ending <code>{state.token_hint}</code>). It is stored on this machine only,
            readable by you alone, and never shown again.
            <button
              type="button"
              className="danger-link"
              disabled={!!busy}
              onClick={() => {
                if (!window.confirm("Forget this bot token? The bot will stop answering.")) return;
                void act("forget", forgetTelegram);
              }}
            >
              Forget it
            </button>
          </p>
        ) : (
          <form
            className="tg-token"
            onSubmit={(event) => {
              event.preventDefault();
              void act("token", async () => {
                const saved = await saveTelegramToken(token.trim());
                setToken("");
                if (saved.username) setNote(`Connected to @${saved.username}.`);
              });
            }}
          >
            <input
              type="password"
              value={token}
              spellCheck={false}
              autoComplete="off"
              placeholder="123456789:AAE..."
              aria-label="Bot token"
              onChange={(event) => setToken(event.target.value)}
            />
            <button type="submit" className="primary-button" disabled={!token.trim() || !!busy}>
              {busy === "token" ? "Checking..." : "Save token"}
            </button>
          </form>
        )}
      </Step>

      <Step n={3} title="Say who may ask" done={paired}>
        <p className="panel-note">
          The bot quotes real source lines from your repo, and anyone can find a bot by name, so it
          only answers chats you name here. Message your bot once from Telegram, then press Find me.
        </p>

        <div className="tg-row">
          <button
            type="button"
            className="side-button"
            disabled={!state.configured || !!busy}
            onClick={() =>
              void act("discover", async () => {
                const chats = await discoverChats();
                setFound(chats);
                if (chats.length === 0) setNote("Nobody has messaged the bot yet. Send it a message, then try again.");
              })
            }
          >
            {busy === "discover" ? "Looking..." : "Find me"}
          </button>
          {running ? <span className="panel-note">Stop the bot to look for new chats.</span> : null}
        </div>

        {found && found.length > 0 ? (
          <ul className="tg-chats">
            {found.map((chat) => {
              const approved = state.chats.includes(chat.id);
              return (
                <li key={chat.id}>
                  <span className="tg-chat-name">{chat.name}</span>
                  <code className="tg-chat-id">{chat.id}</code>
                  <button
                    type="button"
                    className={approved ? "danger-link" : "picker-choose"}
                    disabled={!!busy}
                    onClick={() =>
                      void act("chats", () =>
                        saveTelegramChats(
                          approved
                            ? state.chats.filter((id) => id !== chat.id)
                            : [...state.chats, chat.id],
                        ),
                      )
                    }
                  >
                    {approved ? "Remove" : "Allow"}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}

        {paired ? (
          <p className="tg-settled">
            {state.chats.length} chat(s) allowed: <code>{state.chats.join(", ")}</code>
          </p>
        ) : null}
      </Step>

      <Step n={4} title="Choose the repo it answers about" done={!!state.agent_id}>
        <p className="panel-note">
          One bot can only serve one repo at a time - Telegram allows a single listener per token.
          You can switch here, or from your phone with <code>/use</code>.
        </p>
        <div className="tg-row">
          {agents.map((agent) => (
            <button
              key={agent.id}
              type="button"
              className={`side-button${agent.id === state.agent_id ? " is-on" : ""}`}
              disabled={!!busy}
              onClick={() => void act("agent", () => saveTelegramAgent(agent.id))}
            >
              {agent.name}
            </button>
          ))}
          {agents.length === 0 ? <span className="panel-note">Add a repo first.</span> : null}
        </div>
      </Step>

      <Step n={5} title="Start the bot" done={running}>
        <div className="tg-row">
          <button
            type="button"
            className={`primary-button${running ? " is-stop" : ""}`}
            disabled={!state.configured || !paired || !!busy}
            onClick={() => void act("run", running ? stopTelegram : startTelegram)}
          >
            {busy === "run" ? "Working..." : running ? "Stop the bot" : "Start the bot"}
          </button>

          <button
            type="button"
            className="side-button"
            disabled={!paired || !!busy}
            onClick={() =>
              void act("test", async () => {
                await sendTelegramTest();
                setNote("Sent. Check Telegram.");
              })
            }
          >
            Send a test message
          </button>
        </div>

        <p className="panel-note">
          {running
            ? `Answering about ${state.agent_name || "the selected repo"}. It keeps running when you close this page, so you can ask from your phone.`
            : "Not running. Nothing will answer until you start it."}
        </p>
        {state.status.detail ? <p className="agent-detail">{state.status.detail}</p> : null}
      </Step>

      <section className="tg-cheatsheet">
        <h3>What you can send it</h3>
        <ul>
          <li><code>what did it just do?</code> a plain description of the change</li>
          <li><code>can we ship it?</code> the verdict, and why</li>
          <li><code>why did you stop it?</code> what went wrong</li>
          <li><code>/rules</code> which of your rules the change breaks</li>
          <li><code>/fix</code> a message you can forward to the coding agent</li>
        </ul>
        <h3>And to drive it</h3>
        <ul>
          <li><code>/agents</code> the repos it can watch</li>
          <li><code>/use 2</code> answer about repo 2 from now on</li>
          <li><code>/watch</code> start watching the current repo</li>
          <li><code>/unwatch</code> stop watching it</li>
        </ul>
        <p className="panel-note">
          It only ever replies to you. It never messages your coding agent, and nothing it does
          changes your code.
        </p>
      </section>
    </div>
  );
}
