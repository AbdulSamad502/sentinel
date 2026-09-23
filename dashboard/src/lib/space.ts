/**
 * Laying out everything the agent touched as a rotatable 3D structure.
 *
 * The flat map answered "how big was this change and where did it land". In
 * three dimensions the *shape* of a change reads at a glance too: one folder
 * rewritten end to end looks nothing like a change smeared across the whole
 * tree, and the difference is obvious before you read a single filename.
 *
 * What is encoded, and nothing else is:
 *
 *   * **structure** - the verdict sits at the centre, each top-level folder
 *     gets a hub around it, and files cluster around their hub. Edges are the
 *     real tree, not decoration.
 *   * **depth in the tree** - how far a file sits from its hub.
 *   * **size** - how many lines changed.
 *   * **colour** - the worst severity found in that file. Colour still means
 *     status and nothing else.
 *
 * Everything here is pure maths with no React, no DOM and no randomness. That
 * last part matters: positions come from sorted order and index alone, so the
 * structure is identical on every poll and does not reshuffle under the reader.
 *
 * Hand-rolled rather than pulling in a 3D library. It is one rotation, one
 * perspective divide and a depth sort - a few dozen lines against ~600KB of
 * dependency, and this way it is testable with `node --test` like everything
 * else in here.
 */

import type { ChangeStatus, FileChange, Finding, Severity } from "../types";

/** Clean is not a severity in the Python vocabulary; it means "nothing found". */
export type NodeSeverity = Severity | "clean";

export type NodeKind = "core" | "hub" | "file";

export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

export interface SpaceNode {
  id: string;
  /** The repo-relative path, for files. Empty for the core and for hubs. */
  path: string;
  name: string;
  group: string;
  kind: NodeKind;
  position: Vec3;
  /** Base drawn radius, before perspective. */
  size: number;
  severity: NodeSeverity;
  status: ChangeStatus | null;
  added: number;
  removed: number;
}

export interface SpaceEdge {
  from: string;
  to: string;
}

export interface SpaceLayout {
  nodes: SpaceNode[];
  edges: SpaceEdge[];
  /** The furthest any node sits from the origin, so a camera can frame it. */
  extent: number;
}

/** How far the folder hubs sit from the verdict at the centre. */
const HUB_RADIUS = 150;

/** How far a file sits from its hub, growing with depth in the tree. */
const FILE_RADIUS = 62;
const DEPTH_STEP = 26;
const MAX_DEPTH = 4;

const MIN_SIZE = 4.5;
const MAX_SIZE = 17;
const HUB_SIZE = 6;
const CORE_SIZE = 26;

/** Above this many changed lines a file is simply "big"; growth stops mattering. */
const BIG_CHANGE = 400;

/** The golden angle, which spreads points on a sphere without them lining up. */
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

const SEVERITY_RANK: Record<NodeSeverity, number> = {
  clean: 0,
  ALLOWED: 0,
  FLAGGED: 1,
  BLOCKED: 2,
};

export function topFolder(path: string): string {
  const parts = path.split("/");
  return parts.length > 1 ? parts[0] : "";
}

export function depthOf(path: string): number {
  return Math.min(path.split("/").length - 1, MAX_DEPTH);
}

/**
 * How big to draw a file.
 *
 * Square-rooted, because a 400-line change is not eighty times more important
 * than a 5-line one and drawing it that way would hide everything else.
 */
export function sizeFor(lines: number): number {
  const share = Math.min(Math.max(lines, 0), BIG_CHANGE) / BIG_CHANGE;
  return MIN_SIZE + (MAX_SIZE - MIN_SIZE) * Math.sqrt(share);
}

/** The worst thing found in this file, or "clean" if nothing was. */
export function severityFor(path: string, findings: Finding[]): NodeSeverity {
  let worst: NodeSeverity = "clean";
  for (const finding of findings) {
    if (finding.path !== path) continue;
    if (SEVERITY_RANK[finding.severity] > SEVERITY_RANK[worst]) worst = finding.severity;
  }
  return worst;
}

/**
 * The `index`-th of `count` points spread evenly over a unit sphere.
 *
 * A Fibonacci spiral rather than anything random: even coverage, no clumping,
 * and completely deterministic, which is what keeps the picture stable between
 * refreshes.
 */
export function spherePoint(index: number, count: number): Vec3 {
  if (count <= 1) return { x: 0, y: 0, z: 1 };

  // The half-step offset matters more than it looks. Without it the first and
  // last points land exactly on the poles, where the ring radius is zero - so
  // four folders came out stacked in a vertical line instead of spread around
  // the sphere, and the whole structure drew as a thin ribbon.
  const y = 1 - ((index + 0.5) / count) * 2;
  const ring = Math.sqrt(Math.max(1 - y * y, 0));
  const theta = GOLDEN_ANGLE * index;

  return { x: Math.cos(theta) * ring, y, z: Math.sin(theta) * ring };
}

function scaled(unit: Vec3, radius: number, origin: Vec3 = { x: 0, y: 0, z: 0 }): Vec3 {
  return {
    x: origin.x + unit.x * radius,
    y: origin.y + unit.y * radius,
    z: origin.z + unit.z * radius,
  };
}

export function lengthOf(point: Vec3): number {
  return Math.sqrt(point.x * point.x + point.y * point.y + point.z * point.z);
}

/**
 * Build the whole structure: the verdict, a hub per folder, a node per file.
 *
 * Groups are ordered by size (largest first, then by name) and files by path,
 * so the same change always produces the same arrangement.
 */
export function layout(changes: FileChange[], findings: Finding[]): SpaceLayout {
  const nodes: SpaceNode[] = [];
  const edges: SpaceEdge[] = [];

  if (changes.length === 0) return { nodes, edges, extent: HUB_RADIUS };

  nodes.push({
    id: "__core__",
    path: "",
    name: "",
    group: "",
    kind: "core",
    position: { x: 0, y: 0, z: 0 },
    size: CORE_SIZE,
    severity: "clean",
    status: null,
    added: 0,
    removed: 0,
  });

  const byGroup = new Map<string, FileChange[]>();
  for (const change of changes) {
    const group = topFolder(change.path);
    const existing = byGroup.get(group);
    if (existing) existing.push(change);
    else byGroup.set(group, [change]);
  }

  const ordered = [...byGroup.entries()].sort(
    (left, right) => right[1].length - left[1].length || left[0].localeCompare(right[0]),
  );

  let extent = HUB_RADIUS;

  ordered.forEach(([group, files], groupIndex) => {
    const hubId = `hub:${group}`;
    const hubPosition = scaled(spherePoint(groupIndex, ordered.length), HUB_RADIUS);

    nodes.push({
      id: hubId,
      path: "",
      name: group || "(root)",
      group,
      kind: "hub",
      position: hubPosition,
      size: HUB_SIZE,
      severity: "clean",
      status: null,
      added: 0,
      removed: 0,
    });
    edges.push({ from: "__core__", to: hubId });

    const sorted = [...files].sort((left, right) => left.path.localeCompare(right.path));

    sorted.forEach((change, fileIndex) => {
      const reach = FILE_RADIUS + depthOf(change.path) * DEPTH_STEP;
      const position = scaled(spherePoint(fileIndex, sorted.length), reach, hubPosition);

      nodes.push({
        id: change.path,
        path: change.path,
        name: change.path.split("/").pop() ?? change.path,
        group,
        kind: "file",
        position,
        size: sizeFor(change.added + change.removed),
        severity: severityFor(change.path, findings),
        status: change.status,
        added: change.added,
        removed: change.removed,
      });
      edges.push({ from: hubId, to: change.path });

      extent = Math.max(extent, lengthOf(position));
    });
  });

  return { nodes, edges, extent };
}

/* --- the camera ----------------------------------------------------------- */

export interface Camera {
  /** Rotation about the vertical axis, in radians. */
  yaw: number;
  /** Rotation about the horizontal axis, in radians. Clamped near the poles. */
  pitch: number;
  zoom: number;
}

export interface Projected {
  x: number;
  y: number;
  /** Perspective factor: >1 is nearer than the origin, <1 is further away. */
  scale: number;
  /** Distance along the view axis. Larger is further away. */
  depth: number;
}

/** How far the eye sits from the middle of the structure. */
export const EYE_DISTANCE = 900;

/** Stops the pitch flipping over the pole, where the structure reads as noise. */
export const MAX_PITCH = Math.PI / 2 - 0.05;

export function clampPitch(pitch: number): number {
  return Math.min(Math.max(pitch, -MAX_PITCH), MAX_PITCH);
}

export function clampZoom(zoom: number): number {
  return Math.min(Math.max(zoom, 0.35), 3.5);
}

/**
 * Turn a point in the structure into a point on the screen.
 *
 * Yaw first, then pitch, then a single perspective divide. Nothing cleverer is
 * needed: this is one rigid body being looked at from outside.
 */
export function project(point: Vec3, camera: Camera): Projected {
  const cosYaw = Math.cos(camera.yaw);
  const sinYaw = Math.sin(camera.yaw);
  const cosPitch = Math.cos(camera.pitch);
  const sinPitch = Math.sin(camera.pitch);

  // Yaw about the Y axis.
  const x1 = point.x * cosYaw - point.z * sinYaw;
  const z1 = point.x * sinYaw + point.z * cosYaw;

  // Pitch about the X axis.
  const y2 = point.y * cosPitch - z1 * sinPitch;
  const z2 = point.y * sinPitch + z1 * cosPitch;

  const scale = (EYE_DISTANCE / (EYE_DISTANCE + z2)) * camera.zoom;
  return { x: x1 * scale, y: y2 * scale, scale, depth: z2 };
}

/**
 * Nodes in the order they must be drawn: furthest first.
 *
 * A painter's sort. Without it a node behind the core would be drawn over it
 * and the structure would read inside out.
 */
export function inDrawOrder<T extends { depth: number }>(items: T[]): T[] {
  return [...items].sort((left, right) => right.depth - left.depth);
}

/** How faint something this far away should be. */
export function depthFade(scale: number): number {
  // Tuned so the far side stays legible rather than vanishing: this is a
  // structure to read, not a photograph.
  return Math.min(Math.max((scale - 0.45) / 0.85, 0.22), 1);
}

/** The viewBox that frames a layout, with room for the largest node and label. */
export function viewBoxFor(extent: number): string {
  // The furthest a node can be thrown by perspective, plus room for its own
  // circle and its label. Tighter than this clips; looser and the structure
  // floats in the middle of an empty frame.
  const nearest = EYE_DISTANCE / Math.max(EYE_DISTANCE - extent, 1);
  const edge = Math.ceil(extent * nearest + MAX_SIZE + 24);
  return `${-edge} ${-edge} ${edge * 2} ${edge * 2}`;
}
