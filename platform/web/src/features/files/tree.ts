import { extOf, type FileEntry, type FileSource } from '@/api/files'

/** One row of the explorer. A leaf with a `source` opens in the editor; a `zip` node loads its members when it opens. */
export type TreeNode = {
  key: string
  name: string
  kind: 'dir' | 'file'
  size?: number
  zip?: string
  source?: FileSource
  children: TreeNode[]
}

const order = (a: TreeNode, b: TreeNode) => (a.kind === b.kind ? a.name.localeCompare(b.name, undefined, { numeric: true }) : a.kind === 'dir' ? -1 : 1)

function sortTree(n: TreeNode) {
  n.children.sort(order)
  n.children.forEach(sortTree)
}

/** Builds folders from slash paths. `leaf` makes the node of a file; `prefix` keeps keys unique across trees. */
function build(items: { path: string; kind: 'dir' | 'file'; size?: number }[], prefix: string, leaf: (path: string, size?: number) => Partial<TreeNode>): TreeNode[] {
  const top: TreeNode = { key: prefix, name: '', kind: 'dir', children: [] }
  const dirs = new Map<string, TreeNode>([['', top]])
  const dir = (path: string): TreeNode => {
    const have = dirs.get(path)
    if (have) return have
    const cut = path.lastIndexOf('/')
    const node: TreeNode = { key: `${prefix}${path}/`, name: path.slice(cut + 1), kind: 'dir', children: [] }
    dir(cut < 0 ? '' : path.slice(0, cut)).children.push(node)
    dirs.set(path, node)
    return node
  }
  for (const it of items) {
    const cut = it.path.lastIndexOf('/')
    if (it.kind === 'dir') {
      dir(it.path)
      continue
    }
    const node: TreeNode = { key: `${prefix}${it.path}`, name: it.path.slice(cut + 1), kind: 'file', size: it.size, children: [], ...leaf(it.path, it.size) }
    dir(cut < 0 ? '' : it.path.slice(0, cut)).children.push(node)
  }
  sortTree(top)
  return top.children
}

export function repoTree(roots: { path: string; entries: FileEntry[] }[]): TreeNode[] {
  return roots.map((r) => {
    const rel = (p: string) => p.slice(r.path.length + 1)
    const children = build(
      r.entries.map((e) => ({ path: rel(e.path), kind: e.kind, size: e.size })),
      `repo:${r.path}/`,
      (p) => {
        const full = `${r.path}/${p}`
        return extOf(p) === 'zip' ? { zip: full } : { source: { kind: 'repo', path: full } }
      },
    )
    return { key: `repo:${r.path}`, name: r.path, kind: 'dir', children } satisfies TreeNode
  })
}

/** The members of a zip; `runs/<run_id>/...` names become folders. */
export const zipTree = (zip: string, members: { name: string; size: number }[]): TreeNode[] =>
  build(
    members.map((m) => ({ path: m.name, kind: 'file' as const, size: m.size })),
    `zip:${zip}!`,
    (p) => ({ source: { kind: 'zip', zip, member: p } }),
  )

/** Keeps the files whose name has the text, with their folders. A zip stays only when its own name has the text. */
export function filterTree(nodes: TreeNode[], text: string): TreeNode[] {
  const t = text.trim().toLowerCase()
  if (!t) return nodes
  return nodes.flatMap((n) => {
    if (n.kind === 'file') return n.name.toLowerCase().includes(t) ? [n] : []
    const children = filterTree(n.children, t)
    return children.length ? [{ ...n, children }] : []
  })
}
