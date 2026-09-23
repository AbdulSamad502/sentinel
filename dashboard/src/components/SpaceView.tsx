/**
 * Everything the agent touched, as a structure you can turn around.
 *
 * The file tree answers "what exactly changed in this file". This answers "what
 * is the shape of what just happened" - the question you have when an agent has
 * rewritten twelve files in ninety seconds and you do not yet know where to
 * look. The verdict sits at the centre, each folder is a hub around it, and
 * files cluster on their hub, so a change confined to one folder and a change
 * smeared across the tree look completely different before you read a name.
 *
 * It sits *beside* the file view rather than replacing it, and shares one
 * selection: clicking a node here selects the same file the tree does.
 *
 * Drag to rotate, wheel to zoom. It also turns slowly on its own, because a
 * still projection of a 3D structure just looks like a bad 2D one - that is the
 * only motion here that is not driven by the reader, and
 * `prefers-reduced-motion` switches it off.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  clampPitch,
  clampZoom,
  depthFade,
  inDrawOrder,
  layout,
  project,
  viewBoxFor,
  type Camera,
  type NodeSeverity,
  type SpaceNode,
} from "../lib/space";
import type { FileChange, Finding, Verdict } from "../types";
import "./SpaceView.css";

interface Props {
  changes: FileChange[];
  findings: Finding[];
  verdict: Verdict | null;
  repoName: string;
  selected: string | null;
  onSelect: (path: string) => void;
}

const SEVERITY_CLASS: Record<NodeSeverity, string> = {
  clean: "node-clean",
  ALLOWED: "node-clean",
  FLAGGED: "node-flagged",
  BLOCKED: "node-blocked",
};

/** Slow enough to read while it moves, fast enough to show it is 3D. */
const IDLE_SPIN = 0.00022;

/** Only label what is near the front, or the picture turns into a word cloud. */
const LABEL_SCALE = 0.98;

function prefersStill(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function SpaceView({ changes, findings, verdict, repoName, selected, onSelect }: Props) {
  const still = prefersStill();
  // The frame is sized for the worst case - a node at full radius held exactly
  // side-on - which a real structure almost never reaches. Starting zoomed in
  // fills it properly; anything unusually wide simply overflows, which is
  // visible rather than clipped.
  const [camera, setCamera] = useState<Camera>({ yaw: 0.6, pitch: -0.32, zoom: 1.55 });
  const [spinning, setSpinning] = useState(!still);
  const [hovered, setHovered] = useState<string | null>(null);
  const dragging = useRef<{ x: number; y: number } | null>(null);

  const { nodes, edges, extent } = useMemo(() => layout(changes, findings), [changes, findings]);

  // The idle turn. Paused while the reader is driving it, and never started at
  // all when they have asked for less motion.
  useEffect(() => {
    if (!spinning || still) return;
    let frame = 0;
    let last = performance.now();

    const tick = (now: number) => {
      const elapsed = now - last;
      last = now;
      setCamera((current) => ({ ...current, yaw: current.yaw + IDLE_SPIN * elapsed }));
      frame = requestAnimationFrame(tick);
    };

    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [spinning, still]);

  const onPointerDown = useCallback((event: React.PointerEvent<SVGSVGElement>) => {
    dragging.current = { x: event.clientX, y: event.clientY };
    setSpinning(false);
    event.currentTarget.setPointerCapture(event.pointerId);
  }, []);

  const onPointerMove = useCallback((event: React.PointerEvent<SVGSVGElement>) => {
    const from = dragging.current;
    if (!from) return;

    const dx = event.clientX - from.x;
    const dy = event.clientY - from.y;
    dragging.current = { x: event.clientX, y: event.clientY };

    setCamera((current) => ({
      ...current,
      yaw: current.yaw + dx * 0.007,
      pitch: clampPitch(current.pitch + dy * 0.007),
    }));
  }, []);

  const onPointerUp = useCallback((event: React.PointerEvent<SVGSVGElement>) => {
    dragging.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }, []);

  const onWheel = useCallback((event: React.WheelEvent<SVGSVGElement>) => {
    setCamera((current) => ({ ...current, zoom: clampZoom(current.zoom * (event.deltaY > 0 ? 0.92 : 1.08)) }));
  }, []);

  const placed = useMemo(() => {
    const byId = new Map<string, { node: SpaceNode; screen: ReturnType<typeof project> }>();
    for (const node of nodes) byId.set(node.id, { node, screen: project(node.position, camera) });
    return byId;
  }, [nodes, camera]);

  const drawnEdges = useMemo(
    () =>
      inDrawOrder(
        edges
          .map((edge) => {
            const from = placed.get(edge.from);
            const to = placed.get(edge.to);
            if (!from || !to) return null;
            return {
              key: `${edge.from}->${edge.to}`,
              x1: from.screen.x,
              y1: from.screen.y,
              x2: to.screen.x,
              y2: to.screen.y,
              depth: (from.screen.depth + to.screen.depth) / 2,
              fade: depthFade((from.screen.scale + to.screen.scale) / 2),
            };
          })
          .filter((edge): edge is NonNullable<typeof edge> => edge !== null),
      ),
    [edges, placed],
  );

  const drawnNodes = useMemo(
    () => inDrawOrder([...placed.values()].map((entry) => ({ ...entry, depth: entry.screen.depth }))),
    [placed],
  );

  if (nodes.length === 0) {
    return (
      <section className="panel space-empty">
        <h2 className="panel-title">Nothing touched yet</h2>
        <p className="panel-note">Files appear here as your coding agent changes them.</p>
      </section>
    );
  }

  const fileCount = nodes.filter((node) => node.kind === "file").length;

  return (
    <section className="panel space" aria-label="A map of everything that changed">
      <div className="space-head">
        <div>
          <h2 className="panel-title">Everything touched</h2>
          <p className="panel-note">
            The verdict is at the centre, each folder is a hub around it, and files sit on their
            hub. Size is how much changed; colour is the worst thing found in it.
          </p>
        </div>
        <button
          type="button"
          className="side-button space-spin"
          aria-pressed={spinning}
          onClick={() => setSpinning((on) => !on)}
        >
          {spinning ? "Pause" : "Rotate"}
        </button>
      </div>

      <div className="space-stage">
        <svg
          viewBox={viewBoxFor(extent)}
          className={`space-svg${dragging.current ? " is-dragging" : ""}`}
          role="img"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onWheel={onWheel}
        >
          <title>{`${fileCount} file(s) changed in ${repoName}`}</title>

          {drawnEdges.map((edge) => (
            <line
              key={edge.key}
              x1={edge.x1}
              y1={edge.y1}
              x2={edge.x2}
              y2={edge.y2}
              className="space-edge"
              style={{ opacity: edge.fade * 0.5 }}
            />
          ))}

          {drawnNodes.map(({ node, screen }) => {
            const fade = depthFade(screen.scale);
            const radius = Math.max(node.size * screen.scale, 1.2);

            if (node.kind === "core") {
              return (
                <g key={node.id} transform={`translate(${screen.x} ${screen.y})`} style={{ opacity: fade }}>
                  <circle r={radius} className={`space-core core-${verdict?.level.toLowerCase() ?? "none"}`} />
                  <text className="space-core-text" y={radius * 0.34} style={{ fontSize: radius * 0.5 }}>
                    {verdict?.level ?? "..."}
                  </text>
                </g>
              );
            }

            if (node.kind === "hub") {
              return (
                <g key={node.id} transform={`translate(${screen.x} ${screen.y})`} style={{ opacity: fade }}>
                  <circle r={radius} className="space-hub" />
                  <text className="space-hub-text" y={-radius - 5} style={{ fontSize: 9 * screen.scale }}>
                    {node.name}
                  </text>
                </g>
              );
            }

            const isSelected = node.path === selected;
            const isHovered = node.path === hovered;
            const label = isSelected || isHovered || screen.scale > LABEL_SCALE;

            return (
              <g
                key={node.id}
                transform={`translate(${screen.x} ${screen.y})`}
                className={`space-node ${SEVERITY_CLASS[node.severity]}${isSelected ? " is-selected" : ""}`}
                style={{ opacity: fade }}
                role="button"
                tabIndex={0}
                aria-label={`${node.path}, ${node.status}, plus ${node.added} minus ${node.removed}`}
                onPointerEnter={() => setHovered(node.path)}
                onPointerLeave={() => setHovered((current) => (current === node.path ? null : current))}
                onClick={() => onSelect(node.path)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(node.path);
                  }
                }}
              >
                {isSelected ? <circle r={radius + 5} className="space-halo" /> : null}
                <circle r={radius} className={`space-dot status-${node.status}`} />
                {label ? (
                  <text
                    className="space-label"
                    y={radius + 10 * screen.scale}
                    style={{ fontSize: Math.max(8 * screen.scale, 6) }}
                  >
                    {node.name}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>

        <p className="space-hint">Drag to rotate &middot; wheel to zoom &middot; click a file to open its diff</p>
      </div>

      <ul className="space-key">
        <li><span className="key-swatch node-clean" /> nothing found</li>
        <li><span className="key-swatch node-flagged" /> a rule flagged</li>
        <li><span className="key-swatch node-blocked" /> a rule blocked</li>
        <li><span className="key-hub" /> a folder</li>
      </ul>
    </section>
  );
}
