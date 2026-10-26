import { useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { runPath } from '@/lib/paths'
import { Archive, ArchiveRestore, Copy, Eye, EyeOff, GitCompare, MoreHorizontal, Plus, Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { RUNS_LIMIT, useRuns, type RunFilters } from '@/api/queries'
import { usePresets, useScenarios } from '@/api/runConfig'
import type { RunSummary } from '@/api/types'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ArchiveAll, ArchiveStatus, ShowArchived, useArchiveActions } from '@/features/archive/ArchiveControls'
import { KindBadge, LabelsText, StatusBadge } from './badges'
import { CloneDialog } from './CloneDialog'
import { useHiddenRuns } from './useHiddenRuns'
import { usd } from '@/lib/format'

const FILTERS = [
  { key: 'kind', label: 'Kind', all: 'All kinds', options: ['research', 'exploratory'] },
  { key: 'status', label: 'Status', all: 'Any status', options: ['queued', 'running', 'paused', 'stopping', 'completed', 'stopped', 'failed', 'recovering'] },
  { key: 'split', label: 'Split', all: 'All splits', options: ['pilot', 'dev', 'test', 'exploratory'] },
  { key: 'condition_id', label: 'Group', all: 'All groups', options: ['C1', 'C2a', 'C2b', 'C3', 'C4', 'LC', 'ADV'] },
] as const

const KEYS = ['kind', 'status', 'split', 'condition_id', 'scenario_id', 'preset_id', 'model', 'created_from', 'created_to'] as const

const ALL = '__all'

function useFilters(): [RunFilters, (key: string, value: string) => void, () => void] {
  const [params, setParams] = useSearchParams()
  const filters = Object.fromEntries(KEYS.flatMap((k) => (params.get(k) ? [[k, params.get(k)]] : []))) as RunFilters
  const set = (key: string, value: string) =>
    setParams((p) => {
      if (value === ALL || value === '') p.delete(key)
      else p.set(key, value)
      return p
    })
  const clear = () => setParams(new URLSearchParams())
  return [filters, set, clear]
}

const money = (n: number | undefined) => (n === undefined ? '–' : usd(n))
const when = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })

function Progress({ r }: { r: RunSummary }) {
  return (
    <span className="tabular-nums">
      {r.t ?? 0}
      <span className="text-muted-foreground">/{r.max_rounds ?? '?'}</span>
    </span>
  )
}

function Lineage({ r }: { r: RunSummary }) {
  if (!r.branch && !r.cloned_from) return null
  return (
    <span className="text-xs text-muted-foreground">
      {r.branch ? (
        <>
          {r.branch.intervention.type} of{' '}
          <Link className="underline underline-offset-2" to={runPath(r.branch.parent_run_id)}>
            {r.branch.parent_run_id}
          </Link>{' '}
          at R{r.branch.fork_t}
        </>
      ) : (
        <>
          clone of{' '}
          <Link className="underline underline-offset-2" to={runPath(r.cloned_from!)}>
            {r.cloned_from}
          </Link>
        </>
      )}
    </span>
  )
}

/** A filter that lists ids from a registry. Hidden when the backend does not serve the registry yet. */
function RegistryFilter({ id, label, all, value, options, onChange }: { id: string; label: string; all: string; value?: string; options?: { id: string; name: string }[]; onChange: (v: string) => void }) {
  if (!options) return null
  return (
    <Select value={value ?? ALL} onValueChange={onChange}>
      <SelectTrigger id={id} className="w-full sm:w-44" aria-label={`Filter by ${label}`}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>{all}</SelectItem>
        {options.map((o) => (
          <SelectItem key={o.id} value={o.id}>
            {o.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function DateFilter({ id, label, value, onChange }: { id: string; label: string; value?: string; onChange: (v: string) => void }) {
  return (
    <label htmlFor={id} className="flex items-center gap-2 text-sm text-muted-foreground">
      {label}
      <Input id={id} type="date" className="h-9 w-40 text-foreground" value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

function RowActions({ r, hidden, onClone, onToggleHidden, onArchive }: { r: RunSummary; hidden: boolean; onClone: () => void; onToggleHidden: () => void; onArchive: () => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="icon" variant="ghost" className="size-8" aria-label={`Actions for ${r.id}`}>
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={onClone}>
          <Copy /> Clone and re-run
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onToggleHidden}>
          {hidden ? <Eye /> : <EyeOff />} {hidden ? 'Show in the list' : 'Hide from the list (this browser)'}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onArchive}>
          {r.archived_at ? <ArchiveRestore /> : <Archive />} {r.archived_at ? 'Restore (server)' : 'Archive (server, for everyone)'}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function SelectBox({ r, checked, onChange }: { r: RunSummary; checked: boolean; onChange: () => void }) {
  return <input type="checkbox" name="compare" value={r.id} className="size-4 accent-current" aria-label={`Select ${r.id} to compare`} checked={checked} onChange={onChange} />
}

/** The run page of an id, opened by the id alone (UX review 3, item 3). The run page shows its own "not found" for an unknown id. */
function RunIdBox() {
  const navigate = useNavigate()
  const [id, setId] = useState('')
  return (
    <form
      role="search"
      aria-label="Open a run by its ID"
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (id.trim()) navigate(`/runs/${encodeURIComponent(id.trim())}`)
      }}>
      <Input aria-label="Run ID" placeholder="Exact run ID" className="h-9 w-64 font-mono" value={id} onChange={(e) => setId(e.target.value)} />
      <Button type="submit" size="sm" variant="outline" disabled={!id.trim()}>
        Open
      </Button>
    </form>
  )
}

export function RunsPage() {
  const [filters, setFilter, clearFilters] = useFilters()
  const [query, setQuery] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const runs = useRuns(showArchived ? { ...filters, include_archived: true } : filters)
  const archive = useArchiveActions('runs')
  const scenarios = useScenarios()
  const presets = usePresets()
  const { hidden, toggle } = useHiddenRuns()
  const [showHidden, setShowHidden] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [cloning, setCloning] = useState<string | null>(null)

  const all = runs.data?.items ?? []
  const hiddenHere = all.filter((r) => hidden.has(r.id)).length
  const needle = query.trim().toLowerCase()
  const named = (r: RunSummary) => !needle || (r.name ?? '').toLowerCase().includes(needle)
  const shown = showHidden ? all : all.filter((r) => !hidden.has(r.id))
  const items = shown.filter(named)
  const models = [...new Set(all.flatMap((r) => r.models ?? []))].sort()
  const select = (id: string) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  const active = Object.keys(filters).length > 0

  const muted = (r: RunSummary) => (hidden.has(r.id) ? 'opacity-60' : '')
  const hiddenTag = (r: RunSummary): ReactNode => hidden.has(r.id) && <span className="rounded border px-1 text-xs text-muted-foreground">hidden</span>
  const actions = (r: RunSummary) => (
    <RowActions r={r} hidden={hidden.has(r.id)} onClone={() => setCloning(r.id)} onToggleHidden={() => toggle(r.id)} onArchive={() => void archive.run(r.archived_at ? 'unarchive' : 'archive', { ids: [r.id] })} />
  )
  const archivedTag = (r: RunSummary): ReactNode => r.archived_at && <span className="rounded border px-1 text-xs text-muted-foreground">archived</span>
  const selectedRuns = all.filter((r) => selected.includes(r.id))
  const toArchive = selectedRuns.filter((r) => !r.archived_at).map((r) => r.id)
  const toRestore = selectedRuns.filter((r) => r.archived_at).map((r) => r.id)

  return (
    <div className="space-y-4 pb-16">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Runs</h1>
          <p className="text-sm text-muted-foreground">
            Every run from the ledger, newest first. The ledger is append-only. Hide changes only this list, in this browser. Archive changes the server list for everyone and deletes nothing.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:order-last">
          <ArchiveAll kind="runs" action="archive" busy={archive.busy} shown={items.filter((r) => !r.archived_at).length} onRun={archive.run} />
          {showArchived && <ArchiveAll kind="runs" action="unarchive" busy={archive.busy} shown={items.filter((r) => r.archived_at).length} onRun={archive.run} />}
          <Button asChild>
            <Link to="/runs/new">
              <Plus /> New run
            </Link>
          </Button>
        </div>
      </div>
      <ArchiveStatus notice={archive.notice} error={archive.error} />

      <div className="space-y-2" role="group" aria-label="Filters">
        <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
          {FILTERS.map((f) => (
            <Select key={f.key} value={filters[f.key] ?? ALL} onValueChange={(v) => setFilter(f.key, v)}>
              <SelectTrigger className="w-full sm:w-36" aria-label={`Filter by ${f.label.toLowerCase()}`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>{f.all}</SelectItem>
                {f.options.map((o) => (
                  <SelectItem key={o} value={o}>
                    {o}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ))}
          <RegistryFilter
            id="filter-scenario"
            label="scenario"
            all="All scenarios"
            value={filters.scenario_id}
            options={scenarios.data?.map((s) => ({ id: s.id, name: s.name ? `${s.name} (${s.id})` : s.id }))}
            onChange={(v) => setFilter('scenario_id', v)}
          />
          <RegistryFilter
            id="filter-preset"
            label="preset"
            all="All presets"
            value={filters.preset_id}
            options={presets.data?.map((p) => ({ id: p.id, name: `${p.name} v${p.version}` }))}
            onChange={(v) => setFilter('preset_id', v)}
          />
          <Select value={filters.model ?? ALL} onValueChange={(v) => setFilter('model', v)}>
            <SelectTrigger className="w-full sm:w-44" aria-label="Filter by model">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All models</SelectItem>
              {[...new Set([...models, ...(filters.model ? [filters.model] : [])])].map((m) => (
                <SelectItem key={m} value={m}>
                  {m}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <DateFilter id="filter-from" label="From" value={filters.created_from} onChange={(v) => setFilter('created_from', v)} />
          <DateFilter id="filter-to" label="To" value={filters.created_to} onChange={(v) => setFilter('created_to', v)} />
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" id="show-hidden" className="size-4 accent-current" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />
            Show hidden runs{hiddenHere ? ` (${hiddenHere})` : ''}
          </label>
          <ShowArchived kind="runs" checked={showArchived} onChange={setShowArchived} />
          {active && (
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <RunIdBox />
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <Search className="size-4" aria-hidden />
            <Input type="search" aria-label="Search by name" placeholder="Search by name" className="h-9 w-56 text-foreground" value={query} onChange={(e) => setQuery(e.target.value)} />
          </label>
        </div>
        {runs.isSuccess && (
          <p className="text-xs text-muted-foreground" aria-label="Result count">
            {items.length} run{items.length === 1 ? '' : 's'} shown{needle ? ` (name contains "${query.trim()}")` : ''}, of {all.length} loaded{!showHidden && hiddenHere ? ` (${hiddenHere} hidden)` : ''}.
            {' '}The list loads at most the newest {RUNS_LIMIT} runs that match the filters
            {all.length >= RUNS_LIMIT ? ': the limit is reached, so older runs are not loaded. Narrow the filters or open a run by its ID.' : '; this list is complete.'} The name search reads only the loaded rows.
          </p>
        )}
      </div>

      {runs.isError && (
        <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
          {runs.error.message}
        </p>
      )}

      {runs.isPending && (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      )}

      {runs.isSuccess && items.length === 0 && (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <p className="font-medium">{shown.length ? 'No loaded run has this name.' : all.length ? 'Every run that matches is hidden.' : 'No runs match these filters.'}</p>
          <p className="text-sm text-muted-foreground">{shown.length ? 'Clear the name search, or open the run by its ID.' : all.length ? 'Select "Show hidden runs" to see them.' : 'Start a run with New run, or clear the filters.'}</p>
        </div>
      )}

      {items.length > 0 && (
        <>
          <div className="hidden rounded-lg border md:block">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8">
                    <span className="sr-only">Compare</span>
                  </TableHead>
                  <TableHead>Run</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Group</TableHead>
                  <TableHead>Labels</TableHead>
                  <TableHead>Split</TableHead>
                  <TableHead>Seed</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Round</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="w-10">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((r) => (
                  <TableRow key={r.id} className={muted(r)}>
                    <TableCell>
                      <SelectBox r={r} checked={selected.includes(r.id)} onChange={() => select(r.id)} />
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-col items-start">
                        <span className="flex items-center gap-2">
                          <Link className="font-mono text-sm underline-offset-2 hover:underline" to={runPath(r.id)}>
                            {r.id}
                          </Link>
                          {hiddenTag(r)}
                          {archivedTag(r)}
                        </span>
                        <Lineage r={r} />
                      </div>
                    </TableCell>
                    <TableCell>
                      <KindBadge kind={r.kind} />
                    </TableCell>
                    <TableCell>{r.condition_id ?? '–'}</TableCell>
                    <TableCell className="text-sm">
                      <LabelsText labels={r.labels} kind={r.kind} />
                    </TableCell>
                    <TableCell>{r.split}</TableCell>
                    <TableCell className="tabular-nums">{r.seed}</TableCell>
                    <TableCell>
                      <StatusBadge status={r.status} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Progress r={r} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{money(r.cost_usd)}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{when(r.created_at)}</TableCell>
                    <TableCell>{actions(r)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <ul className="space-y-2 md:hidden">
            {items.map((r) => (
              <li key={r.id} className={`rounded-lg border p-3 ${muted(r)}`}>
                <div className="flex items-center gap-2">
                  <SelectBox r={r} checked={selected.includes(r.id)} onChange={() => select(r.id)} />
                  <Link className="min-w-0 flex-1 truncate font-mono text-sm underline underline-offset-2" to={runPath(r.id)}>
                    {r.id}
                  </Link>
                  {hiddenTag(r)}
                  {archivedTag(r)}
                  <StatusBadge status={r.status} />
                  {actions(r)}
                </div>
                <Lineage r={r} />
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                  <KindBadge kind={r.kind} />
                  <span>{r.condition_id ?? 'no group'}</span>
                  <span>{r.split}</span>
                  <span>seed {r.seed}</span>
                  <span>
                    R<Progress r={r} />
                  </span>
                  <span className="tabular-nums">{money(r.cost_usd)}</span>
                </div>
                <div className="mt-1 text-sm">
                  <LabelsText labels={r.labels} kind={r.kind} />
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {selected.length > 0 && (
        <div className="fixed inset-x-0 bottom-0 z-20 border-t bg-background/95 backdrop-blur">
          <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-3 px-4 py-2">
            <span className="text-sm">
              {selected.length} run{selected.length === 1 ? '' : 's'} selected
            </span>
            {selected.length < 2 ? (
              <Button size="sm" disabled>
                <GitCompare /> Compare (select 2 or more)
              </Button>
            ) : (
              <Button asChild size="sm">
                <Link to={`/runs/compare?ids=${selected.join(',')}`}>
                  <GitCompare /> Compare {selected.length} runs
                </Link>
              </Button>
            )}
            {toArchive.length > 0 && (
              <Button variant="outline" size="sm" disabled={archive.busy} onClick={() => void archive.run('archive', { ids: toArchive }).then((ok) => ok && setSelected([]))}>
                <Archive /> Archive {toArchive.length} selected
              </Button>
            )}
            {toRestore.length > 0 && (
              <Button variant="outline" size="sm" disabled={archive.busy} onClick={() => void archive.run('unarchive', { ids: toRestore }).then((ok) => ok && setSelected([]))}>
                <ArchiveRestore /> Restore {toRestore.length} selected
              </Button>
            )}
            <Button variant="ghost" size="sm" onClick={() => setSelected([])}>
              Clear selection
            </Button>
          </div>
        </div>
      )}

      {cloning && <CloneDialog runId={cloning} onClose={() => setCloning(null)} />}
    </div>
  )
}
