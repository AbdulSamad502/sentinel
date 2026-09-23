/**
 * Choosing the folder to supervise.
 *
 * A browser cannot hand a web page a real filesystem path, so the server does
 * the walking and this shows the result. Folders only: you are choosing
 * somewhere to watch, and offering files would only invite picking one.
 *
 * A folder that already carries a `sentinel.config.json` is marked, because
 * re-adding a project you set up months ago should not feel like starting over.
 */

import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useState } from "react";
import { addAgent, fetchBrowse } from "../api";
import type { Agent, Browse } from "../types";
import "./FolderPicker.css";

interface Props {
  onAdded: (agent: Agent) => void;
  onClose: () => void;
}

export function FolderPicker({ onAdded, onClose }: Props) {
  const [browse, setBrowse] = useState<Browse | null>(null);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const go = useCallback(async (path: string) => {
    try {
      const next = await fetchBrowse(path);
      setBrowse(next);
      setTyped(next.path);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void go("");
  }, [go]);

  const add = useCallback(
    async (path: string) => {
      setBusy(true);
      try {
        onAdded(await addAgent(path));
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setBusy(false);
      }
    },
    [onAdded],
  );

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [onClose]);

  return (
    <div className="picker-backdrop" role="dialog" aria-modal="true" aria-label="Choose a folder">
      <motion.div
        className="picker"
        initial={{ opacity: 0, y: 8, scale: 0.99 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.16, ease: "easeOut" }}
      >
        <header className="picker-head">
          <h2>Which folder should Sentinel watch?</h2>
          <button type="button" className="link-button" onClick={onClose}>
            Close
          </button>
        </header>

        <form
          className="picker-path"
          onSubmit={(event) => {
            event.preventDefault();
            void go(typed);
          }}
        >
          <input
            type="text"
            value={typed}
            spellCheck={false}
            aria-label="Folder path"
            onChange={(event) => setTyped(event.target.value)}
          />
          <button type="submit" className="side-button">
            Go
          </button>
        </form>

        <div className="picker-crumbs">
          <button type="button" className="link-button" onClick={() => void go(browse?.home ?? "")}>
            Home
          </button>
          {browse?.parent ? (
            <button type="button" className="link-button" onClick={() => void go(browse.parent)}>
              Up one
            </button>
          ) : null}
        </div>

        {error ? <p className="picker-error">{error}</p> : null}

        <ul className="picker-list">
          <AnimatePresence initial={false}>
            {(browse?.entries ?? []).map((entry) => (
              <motion.li
                key={entry.path}
                layout
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.12 }}
              >
                <button type="button" className="picker-entry" onClick={() => void go(entry.path)}>
                  <span className="picker-name">{entry.name}</span>
                  {entry.is_git ? <span className="picker-tag">git</span> : null}
                  {entry.configured ? <span className="picker-tag is-set">has rules</span> : null}
                </button>
                <button
                  type="button"
                  className="picker-choose"
                  disabled={busy}
                  onClick={() => void add(entry.path)}
                >
                  Watch this
                </button>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>

        {browse && browse.entries.length === 0 ? (
          <p className="picker-empty">No sub-folders here.</p>
        ) : null}

        <footer className="picker-foot">
          <span className="picker-here" title={browse?.path}>
            {browse?.path}
          </span>
          <button
            type="button"
            className="primary-button"
            disabled={busy || !browse}
            onClick={() => void add(browse?.path ?? "")}
          >
            Watch this folder
          </button>
        </footer>
      </motion.div>
    </div>
  );
}
