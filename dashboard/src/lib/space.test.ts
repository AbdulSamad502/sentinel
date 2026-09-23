/**
 * Tests for the 3D layout and projection.
 *
 * The properties a reader depends on without being told: the structure is the
 * real tree, it does not reshuffle between refreshes, depth reads as distance,
 * a bigger change draws bigger, and things behind other things are drawn first.
 *
 *     npm test
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  clampPitch,
  clampZoom,
  depthFade,
  depthOf,
  inDrawOrder,
  layout,
  lengthOf,
  project,
  severityFor,
  sizeFor,
  spherePoint,
  topFolder,
  viewBoxFor,
  MAX_PITCH,
} from "./space.ts";
import type { FileChange, Finding } from "../types.ts";

function change(path: string, added = 10, removed = 0): FileChange {
  return { path, status: "modified", added, removed, diff: "", diff_truncated: false };
}

function finding(path: string, severity: Finding["severity"]): Finding {
  return {
    norm_id: "a-rule",
    statement: "A rule.",
    path,
    line: 1,
    severity,
    source: "pattern",
    evidence: "x",
  };
}

const files = (result: ReturnType<typeof layout>) => result.nodes.filter((node) => node.kind === "file");

test("a file's top folder is what groups it", () => {
  assert.equal(topFolder("src/pay/charge.py"), "src");
  assert.equal(topFolder("README.md"), "");
});

test("depth is how far from the root, and stops counting eventually", () => {
  assert.equal(depthOf("a.py"), 0);
  assert.equal(depthOf("src/a.py"), 1);
  assert.equal(depthOf("a/b/c/d/e/f/g/h.py"), 4);
});

test("size grows with the change but flattens, so one huge file cannot hide the rest", () => {
  assert.ok(sizeFor(0) < sizeFor(50));
  assert.ok(sizeFor(50) < sizeFor(400));
  assert.ok(sizeFor(4000) - sizeFor(400) < 0.001);
  assert.equal(sizeFor(-5), sizeFor(0), "a nonsense count must not draw a negative circle");
});

test("the worst finding in a file is the one that colours it", () => {
  assert.equal(severityFor("src/a.py", []), "clean");
  assert.equal(
    severityFor("src/a.py", [finding("src/a.py", "FLAGGED"), finding("src/a.py", "BLOCKED")]),
    "BLOCKED",
  );
});

test("another file's findings never colour this one", () => {
  assert.equal(severityFor("src/a.py", [finding("src/b.py", "BLOCKED")]), "clean");
});

test("sphere points are spread out and always on the unit sphere", () => {
  for (const count of [1, 2, 7, 40]) {
    for (let index = 0; index < count; index++) {
      const point = spherePoint(index, count);
      assert.ok(Math.abs(lengthOf(point) - 1) < 1e-9, `point ${index} of ${count} left the sphere`);
    }
  }
});

test("no sphere point sits on a pole, where the ring collapses to nothing", () => {
  // Without the half-step offset, four folders came out stacked in a vertical
  // line and the structure drew as a thin ribbon.
  for (const count of [2, 3, 4, 6]) {
    for (let index = 0; index < count; index++) {
      const point = spherePoint(index, count);
      const ring = Math.sqrt(point.x * point.x + point.z * point.z);
      assert.ok(ring > 0.3, `point ${index} of ${count} has almost no horizontal spread`);
    }
  }
});

test("a handful of folders spread in every direction, not just vertically", () => {
  const spread = (pick: (p: { x: number; y: number; z: number }) => number) => {
    const values = [0, 1, 2, 3].map((index) => pick(spherePoint(index, 4)));
    return Math.max(...values) - Math.min(...values);
  };
  assert.ok(spread((p) => p.x) > 1, "no horizontal spread at all");
  assert.ok(spread((p) => p.z) > 1, "no spread in depth at all");
});

test("no two sphere points land on top of each other", () => {
  const seen = new Set<string>();
  for (let index = 0; index < 30; index++) {
    const point = spherePoint(index, 30);
    const key = `${point.x.toFixed(4)},${point.y.toFixed(4)},${point.z.toFixed(4)}`;
    assert.ok(!seen.has(key), "two nodes would be drawn in exactly the same place");
    seen.add(key);
  }
});

test("an empty change lays out to nothing rather than throwing", () => {
  const result = layout([], []);
  assert.deepEqual(result.nodes, []);
  assert.deepEqual(result.edges, []);
  assert.ok(result.extent > 0, "the viewBox still needs a sane extent");
});

test("every changed file gets exactly one node, plus a core and a hub per folder", () => {
  const result = layout([change("src/a.py"), change("src/b.py"), change("docs/c.md")], []);

  assert.equal(files(result).length, 3);
  assert.equal(result.nodes.filter((node) => node.kind === "core").length, 1);
  assert.equal(result.nodes.filter((node) => node.kind === "hub").length, 2);
});

test("the edges are the real tree: core to hub, hub to file", () => {
  const result = layout([change("src/a.py"), change("docs/c.md")], []);

  assert.ok(result.edges.some((edge) => edge.from === "__core__" && edge.to === "hub:src"));
  assert.ok(result.edges.some((edge) => edge.from === "hub:src" && edge.to === "src/a.py"));
  // Every file hangs off something.
  for (const file of files(result)) {
    assert.ok(result.edges.some((edge) => edge.to === file.id), `${file.id} floats free`);
  }
});

test("files sit near their own folder's hub, not somebody else's", () => {
  const result = layout([change("src/a.py"), change("src/b.py"), change("docs/c.md")], []);
  const hubs = new Map(result.nodes.filter((n) => n.kind === "hub").map((n) => [n.group, n.position]));

  for (const file of files(result)) {
    const own = hubs.get(file.group);
    const other = [...hubs.entries()].find(([group]) => group !== file.group)?.[1];
    const near = lengthOf({
      x: file.position.x - own.x,
      y: file.position.y - own.y,
      z: file.position.z - own.z,
    });
    const far = lengthOf({
      x: file.position.x - other.x,
      y: file.position.y - other.y,
      z: file.position.z - other.z,
    });
    assert.ok(near < far, `${file.id} is nearer another folder's hub than its own`);
  }
});

test("deeper files sit further from their hub", () => {
  const result = layout([change("src/a.py"), change("src/deep/nested/b.py")], []);
  const hub = result.nodes.find((node) => node.kind === "hub").position;

  const reach = (path: string) => {
    const node = files(result).find((file) => file.id === path).position;
    return lengthOf({ x: node.x - hub.x, y: node.y - hub.y, z: node.z - hub.z });
  };
  assert.ok(reach("src/deep/nested/b.py") > reach("src/a.py"));
});

test("the same change lays out identically twice, so a poll does not reshuffle it", () => {
  const changes = [change("src/b.py"), change("src/a.py"), change("docs/c.md")];
  const first = layout(changes, []);
  // Same files, different order in from the server.
  const second = layout([changes[2], changes[1], changes[0]], []);
  assert.deepEqual(first.nodes, second.nodes);
  assert.deepEqual(first.edges, second.edges);
});

test("the busiest folder is placed first, so the picture stays comparable", () => {
  const result = layout([change("docs/c.md"), change("src/a.py"), change("src/b.py")], []);
  const hubs = result.nodes.filter((node) => node.kind === "hub");
  assert.equal(hubs[0].group, "src");
});

test("a blocked file is coloured by its worst finding", () => {
  const result = layout([change("src/a.py")], [finding("src/a.py", "BLOCKED")]);
  assert.equal(files(result)[0].severity, "BLOCKED");
});

test("projection puts the origin in the middle whichever way the camera faces", () => {
  for (const yaw of [0, 1, -2.4]) {
    const screen = project({ x: 0, y: 0, z: 0 }, { yaw, pitch: 0.3, zoom: 1 });
    assert.ok(Math.abs(screen.x) < 1e-9);
    assert.ok(Math.abs(screen.y) < 1e-9);
  }
});

test("rotating by a full turn brings a point back where it started", () => {
  const point = { x: 40, y: -15, z: 90 };
  const before = project(point, { yaw: 0.4, pitch: 0.1, zoom: 1 });
  const after = project(point, { yaw: 0.4 + Math.PI * 2, pitch: 0.1, zoom: 1 });
  assert.ok(Math.abs(before.x - after.x) < 1e-6);
  assert.ok(Math.abs(before.y - after.y) < 1e-6);
});

test("something nearer the eye draws bigger than the same thing further away", () => {
  const near = project({ x: 0, y: 0, z: -300 }, { yaw: 0, pitch: 0, zoom: 1 });
  const far = project({ x: 0, y: 0, z: 300 }, { yaw: 0, pitch: 0, zoom: 1 });
  assert.ok(near.scale > 1 && far.scale < 1, "perspective is not being applied");
  assert.ok(near.depth < far.depth);
});

test("zoom scales everything and is bounded so it cannot be lost", () => {
  const plain = project({ x: 100, y: 0, z: 0 }, { yaw: 0, pitch: 0, zoom: 1 });
  const zoomed = project({ x: 100, y: 0, z: 0 }, { yaw: 0, pitch: 0, zoom: 2 });
  assert.ok(Math.abs(zoomed.x - plain.x * 2) < 1e-9);

  assert.equal(clampZoom(99), 3.5);
  assert.equal(clampZoom(0.001), 0.35);
});

test("pitch cannot flip over the pole", () => {
  assert.equal(clampPitch(9), MAX_PITCH);
  assert.equal(clampPitch(-9), -MAX_PITCH);
  assert.equal(clampPitch(0.2), 0.2);
});

test("things behind other things are drawn first", () => {
  const sorted = inDrawOrder([{ depth: -50 }, { depth: 120 }, { depth: 10 }]);
  assert.deepEqual(sorted.map((item) => item.depth), [120, 10, -50]);
});

test("the far side fades but never disappears", () => {
  assert.ok(depthFade(2) > depthFade(0.5));
  assert.ok(depthFade(0.01) >= 0.22, "the back of the structure must stay readable");
  assert.ok(depthFade(99) <= 1);
});

test("the viewBox is square and contains the whole structure", () => {
  const { extent } = layout([change("a/b/c/d.py")], []);
  const [x, y, width, height] = viewBoxFor(extent).split(" ").map(Number);
  assert.ok(Math.abs(x) > extent, "the outermost node would be clipped");
  assert.equal(x, y);
  assert.equal(width, height);
  assert.equal(width, Math.abs(x) * 2);
});
