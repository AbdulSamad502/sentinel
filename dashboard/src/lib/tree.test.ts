/**
 * Tests for the file navigator's tree.
 *
 * Run with Node's own test runner - it strips the types itself, so this costs
 * no new dependency (instructions.md #4):
 *
 *     npm test
 *
 * The two behaviours worth pinning are the ones that make the navigator feel
 * like a file manager rather than an indented list: folders inherit the worst
 * thing inside them, and a folder holding one folder folds into a single row.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { allFolders, buildTree, firstFile, flatten } from "./tree.ts";
import type { FileChange, Finding, Severity } from "../types.ts";

function change(path: string, status: FileChange["status"] = "modified"): FileChange {
  return { path, status, added: 1, removed: 0, diff: "", diff_truncated: false };
}

function finding(path: string, severity: Severity): Finding {
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

test("nests files under their folders", () => {
  const tree = buildTree([change("src/a.py"), change("src/b.py")], []);
  assert.equal(tree.length, 1);
  assert.equal(tree[0].kind, "folder");
  assert.equal(tree[0].label, "src");
  assert.deepEqual(tree[0].children.map((child) => child.label), ["a.py", "b.py"]);
});

test("folds a chain of single-child folders into one row", () => {
  // `src/payments/charge.py` should read as `src/payments` then the file, not
  // as two nested rows each holding one item.
  const tree = buildTree([change("src/payments/charge.py")], []);
  assert.equal(tree.length, 1);
  assert.equal(tree[0].label, "src/payments");
  assert.equal(tree[0].path, "src/payments");
  assert.equal(tree[0].children[0].label, "charge.py");
});

test("stops folding when a folder holds more than one thing", () => {
  const tree = buildTree([change("src/a/one.py"), change("src/b/two.py")], []);
  assert.equal(tree[0].label, "src");
  assert.deepEqual(tree[0].children.map((child) => child.label), ["a", "b"]);
});

test("a folder inherits the worst severity inside it", () => {
  // Otherwise a problem could hide behind a collapsed chevron.
  const tree = buildTree(
    [change("src/a/one.py"), change("src/b/two.py")],
    [finding("src/a/one.py", "FLAGGED"), finding("src/b/two.py", "BLOCKED")],
  );
  assert.equal(tree[0].severity, "BLOCKED");
});

test("a folder counts every finding beneath it", () => {
  const tree = buildTree(
    [change("src/a/one.py"), change("src/b/two.py")],
    [finding("src/a/one.py", "FLAGGED"), finding("src/a/one.py", "FLAGGED"), finding("src/b/two.py", "BLOCKED")],
  );
  assert.equal(tree[0].findings, 3);
});

test("a clean folder carries no severity at all", () => {
  const tree = buildTree([change("src/a.py")], []);
  assert.equal(tree[0].severity, undefined);
  assert.equal(tree[0].findings, 0);
});

test("folders sort before files, then alphabetically", () => {
  const tree = buildTree([change("zebra.py"), change("alpha.py"), change("src/x.py")], []);
  assert.deepEqual(tree.map((node) => node.label), ["src", "alpha.py", "zebra.py"]);
});

test("a root-level file has no folder wrapper", () => {
  const tree = buildTree([change(".env")], []);
  assert.equal(tree[0].kind, "file");
  assert.equal(tree[0].label, ".env");
  assert.equal(tree[0].depth, 0);
});

test("flatten shows only what is open", () => {
  const tree = buildTree([change("src/a.py"), change("src/b.py")], []);
  assert.equal(flatten(tree, new Set()).length, 1, "a closed folder hides its children");
  assert.equal(flatten(tree, new Set(["src"])).length, 3);
});

test("flatten keeps depth-first order so arrow keys move sensibly", () => {
  const tree = buildTree([change("src/a.py"), change("zz.py")], []);
  const rows = flatten(tree, new Set(allFolders(tree)));
  assert.deepEqual(rows.map((row) => row.label), ["src", "a.py", "zz.py"]);
});

test("allFolders finds nested folders too", () => {
  const tree = buildTree([change("src/a/one.py"), change("src/b/two.py")], []);
  assert.deepEqual(allFolders(tree).sort(), ["src", "src/a", "src/b"]);
});

test("firstFile reaches into folders so something is selected on load", () => {
  const tree = buildTree([change("src/deep/one.py")], []);
  assert.equal(firstFile(tree)?.path, "src/deep/one.py");
});

test("an empty change set produces an empty tree", () => {
  assert.deepEqual(buildTree([], []), []);
  assert.equal(firstFile([]), undefined);
});

test("a deleted file still appears, so it can be selected and recovered", () => {
  const tree = buildTree([change("src/gone.py", "deleted")], []);
  // `src` is not folded away: folding only collapses a folder whose single
  // child is another folder, so a file always sits visibly inside its own.
  assert.equal(tree[0].label, "src");
  const file = tree[0].children[0];
  assert.equal(file.kind, "file");
  assert.equal(file.change?.status, "deleted");
});
