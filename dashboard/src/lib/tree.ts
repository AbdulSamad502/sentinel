/**
 * Turning flat repo-relative paths into a navigable tree.
 *
 * Sentinel reports `src/payments/charge.py`. People think in folders, so the
 * navigator rebuilds the hierarchy and lets them walk it the way they walk a
 * Finder or Explorer window.
 *
 * Two rules make it feel right rather than merely correct:
 *
 *  - **Folders inherit the worst thing inside them.** A collapsed folder still
 *    shows that something in it is a problem, so nothing hides behind a closed
 *    chevron.
 *  - **Single-child folders collapse into one row.** `src/payments/charge.py`
 *    reads as `src/payments` then the file, not three nested rows each holding
 *    one item. Explorer and Finder both do this and it saves a lot of squinting.
 */

import type { FileChange, Finding, Severity } from "../types";

export interface TreeNode {
  /** Full repo-relative path. Unique, and the key for selection. */
  path: string;
  /** What to show on the row - a folder may cover several path segments. */
  label: string;
  kind: "folder" | "file";
  depth: number;
  children: TreeNode[];
  change?: FileChange;
  /** Worst severity at or below this node, so a closed folder still warns. */
  severity?: Severity;
  findings: number;
}

const RANK: Record<Severity, number> = { ALLOWED: 0, FLAGGED: 1, BLOCKED: 2 };

function worse(a: Severity | undefined, b: Severity | undefined): Severity | undefined {
  if (!a) return b;
  if (!b) return a;
  return RANK[a] >= RANK[b] ? a : b;
}

interface Building {
  name: string;
  children: Map<string, Building>;
  change?: FileChange;
}

function emptyNode(name: string): Building {
  return { name, children: new Map() };
}

/**
 * Build the tree. `findings` decide each file's severity, so a folder can roll
 * up the worst rule broken beneath it.
 */
export function buildTree(changes: FileChange[], findings: Finding[]): TreeNode[] {
  const worstByPath = new Map<string, Severity>();
  const countByPath = new Map<string, number>();
  for (const finding of findings) {
    worstByPath.set(finding.path, worse(worstByPath.get(finding.path), finding.severity)!);
    countByPath.set(finding.path, (countByPath.get(finding.path) ?? 0) + 1);
  }

  const root = emptyNode("");
  for (const change of changes) {
    const segments = change.path.split("/").filter(Boolean);
    let cursor = root;
    segments.forEach((segment, index) => {
      let next = cursor.children.get(segment);
      if (!next) {
        next = emptyNode(segment);
        cursor.children.set(segment, next);
      }
      if (index === segments.length - 1) next.change = change;
      cursor = next;
    });
  }

  const convert = (node: Building, prefix: string, label: string, depth: number): TreeNode => {
    const path = prefix ? `${prefix}/${node.name}` : node.name;

    if (node.change) {
      return {
        path,
        label,
        kind: "file",
        depth,
        children: [],
        change: node.change,
        severity: worstByPath.get(path),
        findings: countByPath.get(path) ?? 0,
      };
    }

    // Fold a folder that holds exactly one folder into a single row, the way
    // Explorer and Finder both do: `src/payments` rather than `src` > `payments`.
    const entries = [...node.children.values()];
    if (entries.length === 1 && !entries[0].change) {
      return convert(entries[0], path, `${label}/${entries[0].name}`, depth);
    }

    const children = sortNodes(entries.map((child) => convert(child, path, child.name, depth + 1)));
    return {
      path,
      label,
      kind: "folder",
      depth,
      children,
      severity: children.reduce<Severity | undefined>((acc, child) => worse(acc, child.severity), undefined),
      findings: children.reduce((total, child) => total + child.findings, 0),
    };
  };

  const top = [...root.children.values()].map((child) => convert(child, "", child.name, 0));
  return sortNodes(top);
}

/** Folders before files, then alphabetical - the ordering every file manager uses. */
function sortNodes(nodes: TreeNode[]): TreeNode[] {
  return nodes.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "folder" ? -1 : 1;
    return a.label.localeCompare(b.label);
  });
}

/** The rows currently visible, given which folders are open. Depth-first. */
export function flatten(nodes: TreeNode[], open: Set<string>): TreeNode[] {
  const rows: TreeNode[] = [];
  const walk = (list: TreeNode[]) => {
    for (const node of list) {
      rows.push(node);
      if (node.kind === "folder" && open.has(node.path)) walk(node.children);
    }
  };
  walk(nodes);
  return rows;
}

/** Every folder path in the tree, for "expand all" and the initial open state. */
export function allFolders(nodes: TreeNode[]): string[] {
  const found: string[] = [];
  const walk = (list: TreeNode[]) => {
    for (const node of list) {
      if (node.kind === "folder") {
        found.push(node.path);
        walk(node.children);
      }
    }
  };
  walk(nodes);
  return found;
}

/** The first file in the tree, so something useful is selected on load. */
export function firstFile(nodes: TreeNode[]): TreeNode | undefined {
  for (const node of nodes) {
    if (node.kind === "file") return node;
    const found = firstFile(node.children);
    if (found) return found;
  }
  return undefined;
}
