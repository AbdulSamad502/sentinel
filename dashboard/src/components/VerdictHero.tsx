/**
 * The verdict, as the thing you see first.
 *
 * "Could not verify" sits here beside the reasons rather than in a footnote,
 * because the whole tool rests on an unverified check never reading as a pass.
 * If it were tucked away, the interface would be quietly contradicting the
 * thing the verdict is promising.
 */

import type { Verdict } from "../types";
import { verdictVar, verdictWash } from "../lib/format";
import "./VerdictHero.css";

export function VerdictHero({ verdict }: { verdict: Verdict }) {
  const colour = verdictVar[verdict.level];

  return (
    <section
      className="verdict"
      style={{ background: verdictWash[verdict.level], borderColor: colour }}
      aria-label="Verdict"
    >
      <div className="verdict-top">
        <span className="verdict-level" style={{ color: colour }}>
          {verdict.level}
        </span>
        <p className="verdict-headline">{verdict.headline.replace(/^[A-Z]+ - /, "")}</p>
      </div>

      <div className="verdict-panels">
        {verdict.reasons.length > 0 && (
          <div className="verdict-panel">
            <h2>Why</h2>
            <ul>
              {verdict.reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          </div>
        )}

        {verdict.conditions.length > 0 && (
          <div className="verdict-panel">
            <h2>Ship only once</h2>
            <ul>
              {verdict.conditions.map((condition, index) => (
                <li key={index}>{condition}</li>
              ))}
            </ul>
          </div>
        )}

        {verdict.unchecked.length > 0 && (
          <div className="verdict-panel verdict-unchecked">
            <h2>Could not verify</h2>
            <ul>
              {verdict.unchecked.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
            <p className="verdict-note">An unverified check is not a pass.</p>
          </div>
        )}
      </div>
    </section>
  );
}
