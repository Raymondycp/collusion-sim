import { useMemo, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight, File, FileArchive, FileCode, FileJson, FileText, Folder, FolderOpen, Image, Search } from 'lucide-react'
import { useFilesTree, useZipMembers, extOf, IMAGE_EXT, sourceKey, type FileSource } from '@/api/files'
import { useExperiment, useExperiments } from '@/api/experiments'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { filterTree, repoTree, zipTree, type TreeNode } from './tree'

export type OpenFn = (source: FileSource, title: string) => void
type Ctx = { open: OpenFn; activeKey: string | null; expanded: Set<string>; toggle: (key: string) => void; filtering: boolean }

function iconOf(name: string) {
  const e = extOf(name)
  if (IMAGE_EXT.includes(e)) return Image
  if (e === 'zip') return FileArchive
  if (['json', 'jsonl', 'ipynb', 'yaml', 'yml'].includes(e)) return FileJson
  if (['py', 'html', 'htm'].includes(e)) return FileCode
  if (['md', 'txt', 'csv', 'tsv', 'log'].includes(e)) return FileText
  return File
}

type RowProps = { depth: number; icon: typeof File; label: ReactNode; chevron?: 'open' | 'closed'; active?: boolean; onClick: () => void; title?: string }

function Row({ depth, icon: Icon, label, chevron, active, onClick, title }: RowProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-expanded={chevron ? chevron === 'open' : undefined}
      className={cn('flex w-full items-center gap-1 py-0.5 pr-2 text-left text-[13px] hover:bg-muted', active && 'bg-muted font-medium')}
      style={{ paddingLeft: 6 + depth * 12 }}
    >
      <span className="flex size-4 shrink-0 items-center justify-center text-muted-foreground">
        {chevron === 'open' ? <ChevronDown className="size-3.5" /> : chevron === 'closed' ? <ChevronRight className="size-3.5" /> : null}
      </span>
      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      <span className="truncate">{label}</span>
    </button>
  )
}

const Note = ({ depth, error, children }: { depth: number; error?: boolean; children: ReactNode }) => (
  <p className={cn('py-0.5 text-xs', error ? 'text-red-600' : 'text-muted-foreground')} style={{ paddingLeft: 30 + depth * 12 }}>
    {children}
  </p>
)

function Nodes({ nodes, depth, ctx }: { nodes: TreeNode[]; depth: number; ctx: Ctx }) {
  return nodes.map((n) => <NodeRow key={n.key} node={n} depth={depth} ctx={ctx} />)
}

function ZipChildren({ node, depth, ctx }: { node: TreeNode; depth: number; ctx: Ctx }) {
  const q = useZipMembers(node.zip!, true)
  const nodes = useMemo(() => (q.data ? zipTree(node.zip!, q.data.members) : []), [q.data, node.zip])
  if (q.isPending) return <Note depth={depth}>Loading...</Note>
  if (q.isError) return <Note depth={depth} error>{q.error.message}</Note>
  return <Nodes nodes={nodes} depth={depth} ctx={ctx} />
}

function NodeRow({ node, depth, ctx }: { node: TreeNode; depth: number; ctx: Ctx }) {
  const isOpen = (node.kind === 'dir' && ctx.filtering) || ctx.expanded.has(node.key)
  if (node.kind === 'dir')
    return (
      <>
        <Row depth={depth} icon={isOpen ? FolderOpen : Folder} label={node.name} chevron={isOpen ? 'open' : 'closed'} onClick={() => ctx.toggle(node.key)} />
        {isOpen && <Nodes nodes={node.children} depth={depth + 1} ctx={ctx} />}
      </>
    )
  if (node.zip)
    return (
      <>
        <Row depth={depth} icon={FileArchive} label={node.name} chevron={isOpen ? 'open' : 'closed'} onClick={() => ctx.toggle(node.key)} title={`${node.size ?? 0} bytes`} />
        {isOpen && <ZipChildren node={node} depth={depth + 1} ctx={ctx} />}
      </>
    )
  const source = node.source!
  return (
    <Row
      depth={depth}
      icon={iconOf(node.name)}
      label={node.name}
      active={ctx.activeKey === sourceKey(source)}
      onClick={() => ctx.open(source, node.name)}
      title={node.size === undefined ? undefined : `${node.size} bytes`}
    />
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(true)
  return (
    <section>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="flex w-full items-center gap-1 bg-muted/60 px-1.5 py-1 text-[11px] font-semibold tracking-wide uppercase">
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        {title}
      </button>
      {open && children}
    </section>
  )
}

function RepoSection({ ctx, filter }: { ctx: Ctx; filter: string }) {
  const q = useFilesTree()
  const nodes = useMemo(() => filterTree(repoTree(q.data?.roots ?? []), filter), [q.data, filter])
  return (
    <Section title="Repository files">
      {q.isPending && <Note depth={-1}>Loading...</Note>}
      {q.isError && <Note depth={-1} error>{q.error.message}</Note>}
      {q.data && nodes.length === 0 && <Note depth={-1}>No file matches.</Note>}
      <Nodes nodes={nodes} depth={0} ctx={{ ...ctx, filtering: filter.trim() !== '' }} />
    </Section>
  )
}

function RunRow({ runId, label, depth, ctx }: { runId: string; label: string; depth: number; ctx: Ctx }) {
  const key = `run:${runId}`
  const isOpen = ctx.expanded.has(key)
  const short = runId.slice(-6)
  return (
    <>
      <Row depth={depth} icon={isOpen ? FolderOpen : Folder} label={`${label} ${short}`} title={runId} chevron={isOpen ? 'open' : 'closed'} onClick={() => ctx.toggle(key)} />
      {isOpen && (
        <>
          <Row depth={depth + 1} icon={FileJson} label="meta" active={ctx.activeKey === `meta:${runId}`} onClick={() => ctx.open({ kind: 'run-meta', runId }, `${short} meta`)} />
          <Row depth={depth + 1} icon={FileJson} label="episode.jsonl" active={ctx.activeKey === `episode:${runId}`} onClick={() => ctx.open({ kind: 'run-episode', runId }, `${short} episode.jsonl`)} />
        </>
      )}
    </>
  )
}

function ExperimentRows({ id, depth, ctx }: { id: string; depth: number; ctx: Ctx }) {
  const q = useExperiment(id)
  if (q.isPending) return <Note depth={depth}>Loading...</Note>
  if (q.isError) return <Note depth={depth} error>{q.error.message}</Note>
  return q.data.cells.map((c) => {
    const key = `cell:${id}:${c.condition_id}:${c.seed}`
    const isOpen = ctx.expanded.has(key)
    return (
      <div key={key}>
        <Row depth={depth} icon={isOpen ? FolderOpen : Folder} label={`${c.condition_id} seed ${c.seed}`} title={c.status} chevron={isOpen ? 'open' : 'closed'} onClick={() => ctx.toggle(key)} />
        {isOpen && (
          <>
            <RunRow runId={c.parent.run_id} label="parent" depth={depth + 1} ctx={ctx} />
            {c.branches.map((b) => (
              <RunRow key={b.run_id} runId={b.run_id} label={`${b.kind} ${b.index}`} depth={depth + 1} ctx={ctx} />
            ))}
          </>
        )}
      </div>
    )
  })
}

function RunsSection({ ctx }: { ctx: Ctx }) {
  const q = useExperiments()
  return (
    <Section title="Platform runs">
      {q.isPending && <Note depth={-1}>Loading...</Note>}
      {q.isError && <Note depth={-1} error>{q.error.message}</Note>}
      {q.data?.length === 0 && <Note depth={-1}>No experiment yet.</Note>}
      {q.data?.map((e) => {
        const key = `exp:${e.id}`
        const isOpen = ctx.expanded.has(key)
        return (
          <div key={e.id}>
            <Row depth={0} icon={isOpen ? FolderOpen : Folder} label={e.name || e.id} title={`${e.id} (${e.status})`} chevron={isOpen ? 'open' : 'closed'} onClick={() => ctx.toggle(key)} />
            {isOpen && <ExperimentRows id={e.id} depth={1} ctx={ctx} />}
          </div>
        )
      })}
    </Section>
  )
}

export function Explorer({ open, activeKey }: { open: OpenFn; activeKey: string | null }) {
  const [filter, setFilter] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggle = (key: string) =>
    setExpanded((s) => {
      const next = new Set(s)
      if (!next.delete(key)) next.add(key)
      return next
    })
  const ctx: Ctx = { open, activeKey, expanded, toggle, filtering: false }
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="relative border-b p-1.5">
        <Search className="pointer-events-none absolute top-1/2 left-3.5 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input aria-label="Filter files" placeholder="Filter files" value={filter} onChange={(e) => setFilter(e.target.value)} className="h-7 pl-7 text-xs" />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-2">
        <RepoSection ctx={ctx} filter={filter} />
        <RunsSection ctx={ctx} />
      </div>
    </div>
  )
}
