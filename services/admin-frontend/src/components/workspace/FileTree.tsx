import { useState, useMemo } from 'react'
import type { FileTreeEntry } from '@/types/api'

export interface TreeNode {
  name: string
  path: string
  is_dir: boolean
  size: number
  children: TreeNode[]
}

export function buildTree(entries: FileTreeEntry[]): TreeNode[] {
  const root: TreeNode[] = []
  const map = new Map<string, TreeNode>()

  // Sort: directories first, then by path
  const sorted = [...entries].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    return a.path.localeCompare(b.path)
  })

  for (const entry of sorted) {
    const parts = entry.path.split('/')
    const name = parts[parts.length - 1]
    const node: TreeNode = { name, path: entry.path, is_dir: entry.is_dir, size: entry.size, children: [] }
    map.set(entry.path, node)

    const parentPath = parts.slice(0, -1).join('/')
    const parent = parentPath ? map.get(parentPath) : undefined
    if (parent) {
      parent.children.push(node)
    } else {
      root.push(node)
    }
  }

  return root
}

export function FileTree({
  entries,
  selectedFile,
  onSelect,
}: {
  entries: FileTreeEntry[]
  selectedFile: string | null
  onSelect: (path: string) => void
}) {
  const tree = useMemo(() => buildTree(entries), [entries])

  if (tree.length === 0) {
    return <p className="text-sm text-muted-foreground">Empty workspace</p>
  }

  return (
    <div>
      {tree.map((node) => (
        <TreeNodeView
          key={node.path}
          node={node}
          selectedFile={selectedFile}
          onSelect={onSelect}
          depth={0}
        />
      ))}
    </div>
  )
}

function TreeNodeView({
  node,
  selectedFile,
  onSelect,
  depth,
}: {
  node: TreeNode
  selectedFile: string | null
  onSelect: (path: string) => void
  depth: number
}) {
  const [expanded, setExpanded] = useState(depth < 1)

  if (node.is_dir) {
    return (
      <div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-sm text-foreground hover:bg-muted/50"
          style={{ paddingLeft: depth * 12 + 4 }}
        >
          <span className="text-xs text-muted-foreground">{expanded ? '▼' : '▶'}</span>
          <span>{node.name}/</span>
        </button>
        {expanded &&
          node.children.map((child) => (
            <TreeNodeView
              key={child.path}
              node={child}
              selectedFile={selectedFile}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
      </div>
    )
  }

  return (
    <button
      onClick={() => onSelect(node.path)}
      className={`flex w-full items-center rounded px-1 py-0.5 text-left text-sm ${
        selectedFile === node.path
          ? 'bg-primary/20 text-primary'
          : 'text-foreground hover:bg-muted/50'
      }`}
      style={{ paddingLeft: depth * 12 + 4 }}
    >
      {node.name}
    </button>
  )
}
