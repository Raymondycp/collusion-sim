import { useCallback, useEffect, useState } from 'react'
import { NavLink, useNavigate, useParams } from 'react-router'
import { Plus, X } from 'lucide-react'
import { useExperiment, useExperiments, type ExperimentSummary } from '@/api/experiments'
import { ApiError } from '@/api/client'
import { useRun, useRuns } from '@/api/queries'
import { useConditions } from '@/api/runConfig'
import { ExperimentStatusBadge } from '@/features/experiments/badges'
import { cn } from '@/lib/utils'
import { SetupPage } from './SetupPage'
import { WorkbenchPage } from './WorkbenchPage'
import { branchLabel, conditionText, experimentLabel, short } from './labels'

const TABS_KEY = 'workbench-tabs'
const RECENT_RUNS = 30

/** The open run tabs of this browser tab. Storage can be missing or blocked; the tabs then last until a reload. */
function useOpenTabs() {
  const [tabs, setTabs] = useState<string[]>(() => {
    try {
      const v: unknown = JSON.parse(sessionStorage.getItem(TABS_KEY) ?? '[]')
      return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
    } catch {
      return []
    }
  })
  useEffect(() => {
    try {
      sessionStorage.setItem(TABS_KEY, JSON.stringify(tabs))
    } catch {
      // Not stored: the tabs stay for this visit only.
    }
  }, [tabs])
  return [tabs, setTabs] as const
}

const linkClass = ({ isActive }: { isActive: boolean }) =>
  cn('flex min-w-0 items-center gap-1.5 rounded px-1.5 py-0.5 text-xs hover:bg-muted', isActive && 'bg-muted font-medium')

function RunLink({ runId, label, title, status }: { runId: string; label: string; title?: string; status: Parameters<typeof ExperimentStatusBadge>[0]['status'] }) {
  if (status === 'planned') {
    return (
      <span className="flex items-center gap-1.5 px-1.5 py-0.5 text-xs text-muted-foreground">
        <span className="truncate">{label}</span> <ExperimentStatusBadge status={status} />
      </span>
    )
  }
  return (
    <NavLink to={`/workbench/${runId}`} className={linkClass} title={title ?? runId}>
      <span className="truncate">{label}</span> <ExperimentStatusBadge status={status} />
    </NavLink>
  )
}

function ExperimentCells({ id }: { id: string }) {
  const q = useExperiment(id)
  const conditions = useConditions()
  // Until the conditions load, a cell counts as having a private channel, as every planned ablation group does now.
  const privateChannel = (conditionId: string) => conditions.data?.find((c) => c.id === conditionId)?.channels.private_pairs ?? true
  if (q.error) return <p className="px-1.5 text-xs text-red-600 dark:text-red-400">{q.error.message}</p>
  if (!q.data) return <p className="px-1.5 text-xs text-muted-foreground">Loading…</p>
  return (
    <ul className="space-y-1 pl-2">
      {q.data.cells.map((c) => (
        <li key={`${c.condition_id}-${c.seed}`}>
          <RunLink runId={c.parent.run_id} label={`${c.condition_id} · seed ${c.seed}`} status={c.parent.status} />
          {c.branches.length > 0 && (
            <ul className="pl-3">
              {c.branches.map((b) => (
                <li key={b.run_id}>
                  <RunLink runId={b.run_id} label={branchLabel(b, q.data.branch_plan, { privateChannel: privateChannel(c.condition_id) })} title={`${b.kind} ${b.index}: ${b.run_id}`} status={b.status} />
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ul>
  )
}

function ExperimentNode({ e }: { e: ExperimentSummary }) {
  // The cells load only when the node is open, so a long experiment list costs one request.
  const [open, setOpen] = useState(false)
  return (
    <li>
      <details onToggle={(ev) => setOpen(ev.currentTarget.open)}>
        <summary className="flex cursor-pointer items-center gap-1.5 rounded px-1.5 py-0.5 text-xs hover:bg-muted">
          <span className="truncate" title={e.id}>
            {e.name ?? short(e.id)}
          </span>
          <span className="text-muted-foreground">{e.split}</span>
          <ExperimentStatusBadge status={e.status} />
        </summary>
        {open && <ExperimentCells id={e.id} />}
      </details>
    </li>
  )
}

/** Left rail (UI-WORKBENCH.md §2): experiments with their cells and branch runs, then the recent runs. A click opens a tab. */
function RunTree() {
  const experiments = useExperiments()
  const runs = useRuns({})
  const all = runs.data?.items ?? []
  const parents = all.filter((r) => !r.branch).slice(0, RECENT_RUNS)
  return (
    <nav aria-label="Runs and experiments" className="space-y-3">
      <section className="space-y-1">
        <h2 className="px-1.5 text-xs font-semibold text-muted-foreground uppercase">Experiments</h2>
        {experiments.error && <p className="px-1.5 text-xs text-red-600 dark:text-red-400">{experiments.error.message}</p>}
        {experiments.data?.length === 0 && <p className="px-1.5 text-xs text-muted-foreground">None yet.</p>}
        <ul className="space-y-0.5">{experiments.data?.map((e) => <ExperimentNode key={e.id} e={e} />)}</ul>
      </section>
      <section className="space-y-1">
        <h2 className="px-1.5 text-xs font-semibold text-muted-foreground uppercase">Recent runs</h2>
        {runs.error && <p className="px-1.5 text-xs text-red-600 dark:text-red-400">{runs.error.message}</p>}
        {runs.data?.items.length === 0 && <p className="px-1.5 text-xs text-muted-foreground">None yet.</p>}
        <ul className="space-y-0.5">
          {parents.map((r) => {
            const exp = experimentLabel(r, all, experiments.data ?? [])
            return (
              <li key={r.id}>
                <RunLink runId={r.id} label={`${short(r.id)} · ${r.condition_id ?? r.kind}${exp ? ` · ${exp}` : ''}`} title={r.name ? `${r.name} (${r.id})` : r.id} status={r.status} />
              </li>
            )
          })}
        </ul>
      </section>
    </nav>
  )
}

const isMissing = (e: unknown) => e instanceof ApiError && e.code === 'not_found'

function RunTab({ runId, onClose, onMissing }: { runId: string; onClose: () => void; onMissing: () => void }) {
  const run = useRun(runId)
  // The same cached lists as the rail, so the label costs no request.
  const runs = useRuns({})
  const experiments = useExperiments()
  const exp = run.data ? experimentLabel(run.data, runs.data?.items ?? [], experiments.data ?? []) : null
  const parent = run.data?.branch ? runs.data?.items.find((r) => r.id === run.data!.branch!.parent_run_id) : undefined
  // A stored tab whose run is gone (another data root, a deleted run) leaves the bar.
  const missing = isMissing(run.error)
  useEffect(() => {
    if (missing) onMissing()
  }, [missing, onMissing])
  return (
    <li className="flex shrink-0 items-center rounded-t-md border border-b-0">
      <NavLink to={`/workbench/${runId}`} title={exp ? `${runId} (${exp})` : runId} className={({ isActive }) => cn('flex items-center gap-1.5 px-2 py-1 text-xs', isActive && 'bg-muted font-medium')}>
        <span className="font-mono">{short(runId)}</span>
        {run.data && <span className="text-muted-foreground">{conditionText(run.data, parent) ?? run.data.kind}</span>}
        {run.data && <ExperimentStatusBadge status={run.data.status} />}
      </NavLink>
      <button type="button" className="px-1 text-muted-foreground hover:text-foreground" aria-label={`Close tab ${runId}`} onClick={onClose}>
        <X className="size-3.5" />
      </button>
    </li>
  )
}

/**
 * The workbench frame (UI-WORKBENCH.md §2, §6 step 4): the run tree on the left, one tab per open run, and `+` for setup.
 * The setup page stays mounted once opened, so its unsent draft survives a switch to a run tab.
 */
export function WorkbenchLayout() {
  const runId = useParams().runId
  const navigate = useNavigate()
  const [tabs, setTabs] = useOpenTabs()
  const [setupSeen, setSetupSeen] = useState(!runId)
  // A run becomes a tab only once the server knows it, so a typo or an old id does not stay in the bar.
  const current = useRun(runId ?? '', { enabled: !!runId })
  const known = !!runId && !!current.data

  useEffect(() => {
    if (!runId) setSetupSeen(true)
    else if (known) setTabs((t) => (t.includes(runId) ? t : [...t, runId]))
  }, [runId, known, setTabs])

  const drop = useCallback((id: string) => setTabs((t) => t.filter((x) => x !== id)), [setTabs])
  const close = (id: string) => {
    const i = Math.max(0, tabs.indexOf(id))
    const rest = tabs.filter((t) => t !== id)
    drop(id)
    if (id === runId) navigate(rest.length ? `/workbench/${rest[Math.min(i, rest.length - 1)]}` : '/workbench/new')
  }

  return (
    <div className="flex flex-col gap-3 lg:flex-row">
      <aside className="max-h-64 shrink-0 overflow-y-auto rounded-lg border p-2 lg:sticky lg:top-3 lg:max-h-[calc(100dvh-6rem)] lg:w-64">
        <RunTree />
      </aside>
      <div className="min-w-0 flex-1 space-y-3">
        <ul aria-label="Open runs" className="flex items-end gap-1 overflow-x-auto border-b">
          <li className="flex shrink-0 items-center rounded-t-md border border-b-0">
            <NavLink
              to="/workbench/new"
              aria-label="New run (setup)"
              className={({ isActive }) => cn('flex items-center gap-1 px-2 py-1 text-xs', isActive && 'bg-muted font-medium')}
            >
              <Plus className="size-3.5" /> New
            </NavLink>
          </li>
          {tabs.map((id) => (
            <RunTab key={id} runId={id} onClose={() => close(id)} onMissing={() => drop(id)} />
          ))}
        </ul>
        {setupSeen && (
          <div hidden={!!runId}>
            <SetupPage />
          </div>
        )}
        {runId && <WorkbenchPage key={runId} onClose={() => close(runId)} />}
      </div>
    </div>
  )
}
