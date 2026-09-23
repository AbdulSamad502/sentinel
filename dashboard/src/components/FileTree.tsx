/**
 * The file navigator: a Finder/Explorer window over what the agent changed.
 *
 * One implementation for every platform. Rather than imitating either one, it
 * uses the affordances both share and everyone already knows: a disclosure
 * chevron, folders before files, indent guides, and arrow-key navigation where
 * Right opens a folder, Left closes it or jumps to the parent.
 *
 * Every row carries its own status, and folders inherit the worst thing inside
 * them, so nothing hides behind a collapsed chevron.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";
import type { TreeNode } from "../lib/tree";
import { allFolders, flatten } from "../lib/tree";
import { severityVar, statusMark, statusVar } from "../lib/format";
import "./FileTree.css";

interface Props {
  tree: TreeNode[];
  open: Set<string>;
  selected: string | null;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
  onSetOpen: (paths: Set<string>) => void;
}

function FolderIcon({ isOpen }: { isOpen: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" className="tree-icon">
      {isOpen ? (
        <path
          d="M1.5 13V4.5A1 1 0 0 1 2.5 3.5h3.2l1.4 1.6h5.4a1 1 0 0 1 1 1V7H4.6a1 1 0 0 0-.95.7L1.9 13z"
          fill="currentColor"
          opacity="0.85"
        />
      ) : (
        <path
          d="M1.5 12.5v-8A1 1 0 0 1 2.5 3.5h3.2l1.4 1.6h5.4a1 1 0 0 1 1 1v6.4a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z"
          fill="currentColor"
          opacity="0.85"
        />
      )}
    </svg>
  );
}

function FileIcon() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" className="tree-icon">
      <path
        d="M4 1.5h5L12.5 5v9.5a1 1 0 0 1-1 1h-7.5a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        opacity="0.75"
      />
      <path d="M8.9 1.7V5.2h3.4" fill="none" stroke="currentColor" strokeWidth="1.2" opacity="0.75" />
    </svg>
  );
}

export function FileTree({ tree, open, selected, onToggle, onSelect, onSetOpen }: Props) {
  const rows = useMemo(() => flatten(tree, open), [tree, open]);
  const listRef = useRef<HTMLDivElement>(null);

  const move = useCallback(
    (from: string, delta: number) => {
      const index = rows.findIndex((row) => row.path === from);
      const next = rows[index + delta];
      if (next) onSelect(next.path);
    },
    [rows, onSelect],
  );

  // Keep the selected row in view when selection moves, but scroll only the
  // tree's own list - `scrollIntoView` would scroll the whole window, and on
  // mount that drags the reader straight past the verdict.
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    const list = listRef.current;
    if (!selected || !list) return;

    const row = list.querySelector<HTMLElement>(`[data-path="${CSS.escape(selected)}"]`);
    if (!row) return;

    const above = row.offsetTop < list.scrollTop;
    const below = row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight;
    if (above) list.scrollTop = row.offsetTop;
    else if (below) list.scrollTop = row.offsetTop + row.offsetHeight - list.clientHeight;
  }, [selected]);

  const onKeyDown = (event: React.KeyboardEvent, node: TreeNode) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        move(node.path, 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        move(node.path, -1);
        break;
      case "ArrowRight":
        event.preventDefault();
        if (node.kind === "folder" && !open.has(node.path)) onToggle(node.path);
        else move(node.path, 1);
        break;
      case "ArrowLeft": {
        event.preventDefault();
        if (node.kind === "folder" && open.has(node.path)) {
          onToggle(node.path);
          break;
        }
        // Jump to the enclosing folder, the way both file managers do.
        const parent = rows.filter((row) => row.kind === "folder" && node.path.startsWith(`${row.path}/`)).pop();
        if (parent) onSelect(parent.path);
        break;
      }
      case "Enter":
      case " ":
        event.preventDefault();
        if (node.kind === "folder") onToggle(node.path);
        break;
    }
  };

  if (!rows.length) {
    return (
      <div className="tree-empty">
        <p>Nothing has changed since Sentinel started watching.</p>
      </div>
    );
  }

  const everyFolder = allFolders(tree);
  const allOpen = everyFolder.every((path) => open.has(path));

  return (
    <div className="tree">
      <div className="tree-head">
        <span className="tree-title">Changed files</span>
        {everyFolder.length > 0 && (
          <button
            type="button"
            className="tree-expand"
            onClick={() => onSetOpen(allOpen ? new Set() : new Set(everyFolder))}
          >
            {allOpen ? "Collapse all" : "Expand all"}
          </button>
        )}
      </div>

      <div className="tree-rows" role="tree" aria-label="Changed files" ref={listRef}>
        {rows.map((node) => {
          const isOpen = open.has(node.path);
          const isSelected = selected === node.path;
          return (
            <div
              key={node.path}
              data-path={node.path}
              role="treeitem"
              aria-level={node.depth + 1}
              aria-selected={isSelected}
              aria-expanded={node.kind === "folder" ? isOpen : undefined}
              tabIndex={isSelected ? 0 : -1}
              className={`tree-row${isSelected ? " is-selected" : ""}`}
              style={{ paddingLeft: `${node.depth * 14 + 8}px`, ["--indent" as string]: `${node.depth * 14}px` }}
              onClick={() => (node.kind === "folder" ? onToggle(node.path) : onSelect(node.path))}
              onKeyDown={(event) => onKeyDown(event, node)}
            >
              <span className="tree-chevron" aria-hidden="true">
                {node.kind === "folder" ? (isOpen ? "▾" : "▸") : ""}
              </span>

              <span className="tree-glyph" style={{ color: node.severity ? severityVar[node.severity] : undefined }}>
                {node.kind === "folder" ? <FolderIcon isOpen={isOpen} /> : <FileIcon />}
              </span>

              <span className="tree-label" title={node.path}>
                {node.label}
              </span>

              {node.findings > 0 && (
                <span
                  className="tree-badge"
                  style={{ color: node.severity ? severityVar[node.severity] : undefined }}
                  title={`${node.findings} rule violation(s)`}
                >
                  {node.findings}
                </span>
              )}

              {node.change && (
                <>
                  <span className="tree-counts">
                    {node.change.added > 0 && <span className="tree-add">+{node.change.added}</span>}
                    {node.change.removed > 0 && <span className="tree-remove">-{node.change.removed}</span>}
                  </span>
                  <span
                    className="tree-status"
                    style={{ color: statusVar[node.change.status] }}
                    title={node.change.status}
                  >
                    {statusMark[node.change.status]}
                  </span>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
