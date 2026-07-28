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
