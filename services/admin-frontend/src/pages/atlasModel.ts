import type { PlacementView, ProductView, Topology } from '../types/api'

export const UNPLACED = 'unplaced'

export interface PlacementGroup {
  handle: string
  placement: PlacementView | null
  products: ProductView[]
}

/** Where a product lives: the placement most of its containers run on. */
export function productPlacement(product: ProductView): string {
  const counts = new Map<string, number>()
  for (const container of product.passport.containers) {
    counts.set(container.placement, (counts.get(container.placement) ?? 0) + 1)
  }
  let best = UNPLACED
  let bestCount = 0
  for (const [handle, count] of counts) {
    if (count > bestCount || (count === bestCount && handle < best)) {
      best = handle
      bestCount = count
    }
  }
  return best
}

/** Product placements in a stable order: by handle, products by title, unplaced last. */
export function groupByPlacement(topology: Topology): PlacementGroup[] {
  const byHandle = new Map<string, PlacementGroup>()
  for (const placement of topology.placements) {
    if (placement.role === 'control') continue
    byHandle.set(placement.handle, { handle: placement.handle, placement, products: [] })
  }
  for (const product of topology.products) {
    const handle = productPlacement(product)
    if (!byHandle.has(handle)) byHandle.set(handle, { handle, placement: null, products: [] })
    byHandle.get(handle)!.products.push(product)
  }
  const groups = [...byHandle.values()]
  for (const group of groups) group.products.sort((a, b) => a.title.localeCompare(b.title))
  return groups.sort((a, b) => {
    if (a.handle === UNPLACED) return 1
    if (b.handle === UNPLACED) return -1
    return a.handle.localeCompare(b.handle)
  })
}

export type SelectionKind = 'product' | 'package' | 'secret' | 'placement'

export interface Selection {
  kind: SelectionKind
  id: string
}

export interface Highlight {
  products: Set<string>
  packages: Set<string>
  secrets: Set<string>
}

/** Selecting a component lights the products using it; selecting a product lights its parts. */
export function highlight(topology: Topology, selection: Selection | null): Highlight | null {
  if (!selection) return null
  const result: Highlight = { products: new Set(), packages: new Set(), secrets: new Set() }
  for (const product of topology.products) {
    const packages = product.passport.packages.map((p) => p.name)
    const uses =
      (selection.kind === 'product' && product.project_id === selection.id) ||
      (selection.kind === 'package' && packages.includes(selection.id)) ||
      (selection.kind === 'secret' && product.passport.user_secrets.includes(selection.id)) ||
      (selection.kind === 'placement' && productPlacement(product) === selection.id)
    if (!uses) continue
    result.products.add(product.project_id)
    if (selection.kind === 'product') {
      packages.forEach((name) => result.packages.add(name))
      product.passport.user_secrets.forEach((name) => result.secrets.add(name))
    }
  }
  if (selection.kind === 'package') result.packages.add(selection.id)
  if (selection.kind === 'secret') result.secrets.add(selection.id)
  return result
}

export interface Usage {
  name: string
  count: number
}

function usage(names: string[][]): Usage[] {
  const counts = new Map<string, number>()
  for (const list of names) for (const name of new Set(list)) counts.set(name, (counts.get(name) ?? 0) + 1)
  return [...counts]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
}

/** Catalog packages and libraries by how many products carry them. */
export function packageUsage(topology: Topology): Usage[] {
  return usage(topology.products.map((p) => p.passport.packages.map((pkg) => pkg.name)))
}

/** External keys (user secrets) by how many products hold them; names only. */
export function secretUsage(topology: Topology): Usage[] {
  return usage(topology.products.map((p) => p.passport.user_secrets))
}

export function packageKind(topology: Topology, name: string): 'package' | 'library' {
  for (const product of topology.products) {
    const found = product.passport.packages.find((p) => p.name === name)
    if (found) return found.kind
  }
  return 'package'
}

/** Worst container status of a product, for its tile's dot. */
export function productHealth(product: ProductView): 'ok' | 'warn' | 'down' | 'idle' {
  const statuses = product.passport.containers.map((c) => c.status)
  if (statuses.length === 0) return 'idle'
  if (statuses.includes('down')) return 'down'
  if (statuses.some((s) => s !== 'running')) return 'warn'
  return 'ok'
}
