/**
 * The rules this project holds its coding agent to.
 *
 * This is the step that used to be a thirteen-question terminal wizard, and it
 * is the reason the dashboard can start an agent at all: rules are declared
 * once, written into that repo's own `sentinel.config.json`, and read on every
 * later run. Nothing asks again.
 *
 * Two things the interface must never blur:
 *
 *   * **How a rule is checked.** A rule with a regex behind it is a fact, free
 *     and offline. A rule the model judges is an opinion and costs a call.
 *     Every row says which it is.
 *   * **Who sets severity.** The human does, here. The model is never asked how
 *     serious anything is.
 */

import { motion } from "framer-motion";
import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchCatalog, fetchNorms, saveNorms } from "../api";
import type { Catalog, ProjectNorms, Severity } from "../types";
import "./NormsEditor.css";

interface Props {
  agentId: string;
  agentName: string;
  onSaved: (norms: ProjectNorms) => void;
}

export function NormsEditor({ agentId, agentName, onSaved }: Props) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [custom, setCustom] = useState("");
  const [severity, setSeverity] = useState<Severity>("FLAGGED");
  const [configured, setConfigured] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [loadedCatalog, current] = await Promise.all([fetchCatalog(), fetchNorms(agentId)]);
        if (cancelled) return;
        setCatalog(loadedCatalog);
        setSelected(new Set(current.selected));
        setCustom(current.custom.join("\n"));
        setSeverity(current.severity);
        setConfigured(current.configured);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  const toggle = useCallback((id: string) => {
    setSaved(false);
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const applyPreset = useCallback((ids: string[]) => {
    setSaved(false);
    setSelected(new Set(ids));
  }, []);

  const save = useCallback(async () => {
    setBusy(true);
    try {
      const lines = custom
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      const result = await saveNorms(agentId, [...selected], lines, severity);
      setConfigured(true);
      setSaved(true);
      onSaved(result);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }, [agentId, custom, onSaved, selected, severity]);

  const counts = useMemo(() => {
    const all = (catalog?.groups ?? []).flatMap((group) => group.norms);
    const chosen = all.filter((norm) => selected.has(norm.id));
    return {
      pattern: chosen.filter((norm) => norm.checked_by === "pattern").length,
      model: chosen.filter((norm) => norm.checked_by === "model").length,
    };
  }, [catalog, selected]);

  if (error && !catalog) return <p className="panel-note">{error}</p>;
  if (!catalog) return <p className="panel-note">Loading the rules...</p>;

  return (
    <div className="norms">
      <header className="norms-head">
        <div>
          <h2>Rules for {agentName}</h2>
          <p className="panel-note">
            {configured
              ? "These are written into this project's own sentinel.config.json. Change them whenever; nothing will ask again."
              : "Pick what this project expects of a coding agent. Saved into the project itself, so it only has to be said once."}
          </p>
        </div>
      </header>

      <section className="norms-block">
        <h3>Start from a set</h3>
        <div className="preset-row">
          {catalog.presets.map((preset) => (
            <button
              key={preset.key}
              type="button"
              className="side-button"
              onClick={() => applyPreset(preset.norm_ids)}
            >
              {preset.label}
            </button>
          ))}
          <button type="button" className="link-button" onClick={() => applyPreset([])}>
            Clear all
          </button>
        </div>
      </section>

      {catalog.groups.map((group) => (
        <section className="norms-block" key={group.title}>
          <h3>{group.title}</h3>
          <ul className="norm-list">
            {group.norms.map((norm) => (
              <li key={norm.id}>
                <label className="norm-row">
                  <input
                    type="checkbox"
                    checked={selected.has(norm.id)}
                    onChange={() => toggle(norm.id)}
                  />
                  <span className="norm-text">
                    <span className="norm-statement">{norm.statement}</span>
                    <span className={`norm-chip chip-${norm.checked_by}`}>
                      {norm.checked_by === "pattern" ? "checked by pattern" : "judged by the model"}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </section>
      ))}

      <section className="norms-block">
        <h3>Your own rules</h3>
        <p className="panel-note">
          The ones only your team knows, one per line. These are judged by the model, because no
          regex catches "do not redesign the dashboard without asking".
        </p>
        <textarea
          className="norms-custom"
          rows={5}
          spellCheck={false}
          value={custom}
          placeholder={"never change the payment flow without asking\ndo not add a dependency for something small"}
          onChange={(event) => {
            setSaved(false);
            setCustom(event.target.value);
          }}
        />
      </section>

      <section className="norms-block">
        <h3>When a rule is broken</h3>
        <div className="severity-row" role="group" aria-label="Severity">
          <button
            type="button"
            className={`toggle-button${severity === "FLAGGED" ? " is-on" : ""}`}
            aria-pressed={severity === "FLAGGED"}
            onClick={() => setSeverity("FLAGGED")}
          >
            Flag for review
          </button>
          <button
            type="button"
            className={`toggle-button${severity === "BLOCKED" ? " is-on" : ""}`}
            aria-pressed={severity === "BLOCKED"}
            onClick={() => setSeverity("BLOCKED")}
          >
            Call STOP
          </button>
        </div>
        <p className="panel-note">
          Rules the model judges can be wrong sometimes, so flagging is the safer default. A rule
          the catalog already treats as non-negotiable stays that way either way.
        </p>
      </section>

      <footer className="norms-foot">
        <span className="panel-note">
          {selected.size + custom.split("\n").filter((line) => line.trim()).length} rule(s) &middot;{" "}
          {counts.pattern} by pattern, {counts.model + custom.split("\n").filter((line) => line.trim()).length} by
          the model
        </span>
        <div className="norms-save">
          {saved ? (
            <motion.span
              className="norms-saved"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.15 }}
            >
              Saved into the project.
            </motion.span>
          ) : null}
          <button type="button" className="primary-button" disabled={busy} onClick={() => void save()}>
            {busy ? "Saving..." : "Save rules"}
          </button>
        </div>
      </footer>

      {error ? <p className="picker-error">{error}</p> : null}
    </div>
  );
}
