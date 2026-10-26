import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { runPath } from '@/lib/paths'
import { Archive, ArchiveRestore, ArrowLeft, Plus, RotateCw } from 'lucide-react'
import { experimentExports } from '@/api/exports'
import { useExperimentEvaluations } from '@/api/judge'
import { ExportMenu } from '@/features/export/ExportMenu'
import { retryExperiment, useExperiment, useExperiments, type Experiment, type ExperimentCell } from '@/api/experiments'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { spendText } from '@/components/budget'
import { ErrorDetailList } from '@/features/workbench/RunHealth'
import { useRun } from '@/api/queries'
import { ArchiveAll, ArchiveStatus, ShowArchived, useArchiveActions } from '@/features/archive/ArchiveControls'
import { ExperimentStatusBadge } from './badges'
import { EstimateView } from './NewExperimentPage'

function Alert({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="text-sm text-red-600 dark:text-red-400">
      {children}
    </p>
  )
}

const Mono = ({ children }: { children: ReactNode }) => <span className="font-mono text-xs break-all">{children}</span>

const countsText = (counts: Record<string, number>) =>
  Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${k}`)
    .join(', ')

export function ExperimentsPage() {
  const [showArchived, setShowArchived] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const list = useExperiments(showArchived)
  const archive = useArchiveActions('experiments')
  const rows = list.data ?? []
  const chosen = rows.filter((x) => selected.includes(x.id))
  const toArchive = chosen.filter((x) => !x.archived_at).map((x) => x.id)
  const toRestore = chosen.filter((x) => x.archived_at).map((x) => x.id)
  const pick = (id: string) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">Experiments</h1>
        <div className="flex flex-wrap items-center gap-2">
          <ArchiveAll kind="experiments" action="archive" busy={archive.busy} shown={rows.filter((x) => !x.archived_at).length} onRun={archive.run} />
          {showArchived && <ArchiveAll kind="experiments" action="unarchive" busy={archive.busy} shown={rows.filter((x) => x.archived_at).length} onRun={archive.run} />}
          <Button asChild size="sm">
            <Link to="/experiments/new">
              <Plus /> New experiment
            </Link>
          </Button>
        </div>
      </div>
      <p className="text-sm text-muted-foreground">An experiment runs conditions × seeds. Each cell is one research run plus its probe, placebo and ablation children. Archive changes this list on the server for everyone and deletes nothing.</p>
      <ShowArchived kind="experiments" checked={showArchived} onChange={setShowArchived} />
      <ArchiveStatus notice={archive.notice} error={archive.error} />
      {(toArchive.length > 0 || toRestore.length > 0) && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border p-2 text-sm">
          <span>{selected.length} selected</span>
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
      )}
      {list.isPending && <Skeleton className="h-32 w-full" />}
      {list.error && <Alert>{list.error.message}</Alert>}
      {list.data?.length === 0 && <p className="text-sm text-muted-foreground">{showArchived ? 'No experiments, archived or not.' : 'No experiments yet.'}</p>}
      {!!list.data?.length && (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/50 text-xs text-muted-foreground">
              <tr>
                <th className="w-8 p-2">
                  <span className="sr-only">Select</span>
                </th>
                <th className="p-2">Experiment</th>
                <th className="p-2">Status</th>
                <th className="p-2">Split</th>
                <th className="p-2">Conditions</th>
                <th className="p-2">Seeds</th>
                <th className="p-2">Cells</th>
                <th className="p-2">Spend</th>
                <th className="p-2">Created</th>
                <th className="p-2">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {list.data.map((x) => (
                <tr key={x.id} className={`border-t align-top ${x.archived_at ? 'opacity-60' : ''}`}>
                  <td className="p-2">
                    <input type="checkbox" className="size-4 accent-current" aria-label={`Select ${x.id} to archive or restore`} checked={selected.includes(x.id)} onChange={() => pick(x.id)} />
                  </td>
                  <td className="p-2">
                    <Link className="font-medium underline-offset-2 hover:underline" to={`/experiments/${x.id}`}>
                      {x.name ?? 'Unnamed'}
                    </Link>
                    {x.archived_at && <span className="ml-2 rounded border px-1 text-xs text-muted-foreground">archived</span>}
                    <span className="block">
                      <Mono>{x.id}</Mono>
                    </span>
                  </td>
                  <td className="p-2">
                    <ExperimentStatusBadge status={x.status} />
                  </td>
                  <td className="p-2">{x.split}</td>
                  <td className="p-2">{x.conditions.join(', ')}</td>
                  <td className="p-2">{x.seeds.length}</td>
                  <td className="p-2 text-xs">{countsText(x.counts)}</td>
                  <td className="p-2 text-xs whitespace-nowrap">
                    {spendText(x.spend)}
                  </td>
                  <td className="p-2 text-xs whitespace-nowrap">{new Date(x.created_at).toLocaleString()}</td>
                  <td className="p-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={archive.busy || (!x.archived_at && x.status === 'running')}
                      title={!x.archived_at && x.status === 'running' ? 'A running experiment cannot be archived. Stop it or wait until it ends.' : undefined}
                      onClick={() => void archive.run(x.archived_at ? 'unarchive' : 'archive', { ids: [x.id] })}>
                      {x.archived_at ? <ArchiveRestore /> : <Archive />} {x.archived_at ? 'Restore' : 'Archive'}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function RunLink({ id, children }: { id: string; children: ReactNode }) {
  return (
    <Link className="underline underline-offset-2" to={runPath(id)}>
      {children}
    </Link>
  )
}

/**
 * Why a parent run failed: its `error_detail`, from one lookup of the run by its id (UX review 2 item 2, CR-104 gap 2). The page has no
 * public view, so the detail always shows. A failed run that has no detail (an older run) shows nothing more than the status line.
 */
function ParentFailure({ runId }: { runId: string }) {
  const run = useRun(runId)
  const detail = run.data?.error_detail
  if (!detail) return null
  return (
    <div className="max-w-64 whitespace-normal">
      <ErrorDetailList detail={detail} />
    </div>
  )
}

function Cell({ c }: { c: ExperimentCell }) {
  return (
    <div className="space-y-1">
      <ExperimentStatusBadge status={c.status} />
      <p>
        <RunLink id={c.parent.run_id}>parent</RunLink> <span className="text-muted-foreground">{c.parent.status}</span>
        {c.parent.end_reason && <span className="text-muted-foreground">, {c.parent.end_reason}</span>}
      </p>
      {c.parent.status === 'failed' && <ParentFailure runId={c.parent.run_id} />}
      {c.branches.length > 0 && (
        <ul className="space-y-0.5">
          {c.branches.map((b) => (
            <li key={b.index}>
              {b.status === 'planned' ? <span>{b.kind}</span> : <RunLink id={b.run_id}>{b.kind}</RunLink>} <span className="text-muted-foreground">{b.status}</span>
            </li>
          ))}
        </ul>
      )}
      {c.error && (
        <div role="alert" className="max-w-64 rounded border border-red-500/40 bg-red-500/10 p-1.5 whitespace-normal">
          <p className="font-medium">{c.error.code}</p>
          <p>{c.error.message}</p>
          <p className="text-muted-foreground">{new Date(c.error.at).toLocaleString()}</p>
        </div>
      )}
    </div>
  )
}

function Grid({ x }: { x: Experiment }) {
  const at = new Map(x.cells.map((c) => [`${c.condition_id}/${c.seed}`, c]))
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="text-left text-xs" aria-label="Cells">
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th className="p-2">Condition</th>
            {x.seeds.map((s) => (
              <th key={s} className="p-2 font-mono">
                seed {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {x.conditions.map((cid) => (
            <tr key={cid} className="border-t align-top">
              <th scope="row" className="p-2 font-medium">
                <Link className="underline-offset-2 hover:underline" to={`/conditions/${cid}`}>
                  {cid}
                </Link>
              </th>
              {x.seeds.map((s) => {
                const c = at.get(`${cid}/${s}`)
                return (
                  <td key={s} className="p-2 whitespace-nowrap" data-cell={`${cid}/${s}`}>
                    {c ? <Cell c={c} /> : <span className="text-muted-foreground">no cell</span>}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * A retry can start planned LLM parents and their branches, so it needs the same paid approval as a start (CR-27 m1).
 * The Experiment does not list its agents, so the stored estimate's calls decide: 0 calls means every seller is scripted.
 */
function RetryDialog({ x, blocked, busy, onRetry, onClose }: { x: Experiment; blocked: ExperimentCell[]; busy: boolean; onRetry: () => void; onClose: () => void }) {
  const [approved, setApproved] = useState(false)
  const left = Math.max(0, x.estimate.calls_max - x.spend.calls)
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Paid retry: {blocked.length} blocked cell{blocked.length === 1 ? '' : 's'}</DialogTitle>
          <DialogDescription>This experiment has LLM agents. A retry can start their runs, and the runs call their providers.</DialogDescription>
        </DialogHeader>
        <ul aria-label="Blocked cells" className="list-disc pl-4 text-sm">
          {blocked.map((c) => (
            <li key={`${c.condition_id}/${c.seed}`}>
              {c.condition_id} seed {c.seed}
            </li>
          ))}
        </ul>
        <label className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-2 text-sm">
          <input type="checkbox" className="mt-0.5 size-4" checked={approved} onChange={(ev) => setApproved(ev.target.checked)} />
          <span>
            Paid model calls: up to {left.toLocaleString()} calls left of the estimate ({x.estimate.calls_max.toLocaleString()}). I have the lead's approval.
          </span>
        </label>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy || !approved} onClick={onRetry}>
            Retry paid runs
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ExperimentPage() {
  const { experimentId = '' } = useParams()
  const q = useExperiment(experimentId)
  const x = q.data
  const blocked = x?.cells.filter((c) => c.status === 'blocked') ?? []
  const qc = useQueryClient()
  const [confirm, setConfirm] = useState(false)
  const [judged, setJudged] = useState<string[]>([])
  const parents = x?.cells.map((c) => c.parent.run_id) ?? []
  const evaluations = useExperimentEvaluations(parents)
  const paid = (x?.estimate.calls_max ?? 0) > 0
  const retry = useMutation({
    mutationFn: () => retryExperiment(experimentId),
    onSuccess: (fresh) => {
      setConfirm(false)
      qc.setQueryData(['experiment', experimentId], fresh)
      return qc.invalidateQueries({ queryKey: ['experiments'] })
    },
  })
  return (
    <div className="space-y-4">
      <Link to="/experiments" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Experiments
      </Link>
      {q.isPending && <Skeleton className="h-64 w-full" />}
      {q.error && <Alert>{q.error.message}</Alert>}
      {x && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-lg font-semibold">{x.name ?? 'Unnamed experiment'}</h1>
            <ExperimentStatusBadge status={x.status} />
            <ExportMenu
              id={x.id}
              items={experimentExports(judged)}
              live={x.status !== 'completed' && x.status !== 'ended'}
              choice={{
                title: 'Judge columns from (completed judge jobs, in the order you check them)',
                options: (evaluations.data ?? []).map((e) => ({ id: e.id, label: `${e.id} · ${e.block} · ${e.view} · ${e.run_ids.filter((r) => parents.includes(r)).length} parents` })),
                selected: judged,
                onChange: setJudged,
              }}
            />
          </div>
          {blocked.length > 0 && (
            <div role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
              <p>
                {blocked.length} cell{blocked.length === 1 ? ' is' : 's are'} blocked: the server could not start the next run. The cells show why.
              </p>
              <p className="mt-1">Fix the cause (for example, turn paid calls on), then retry. Runs that ended are not run again.</p>
              <Button size="sm" variant="outline" className="mt-2" disabled={retry.isPending} onClick={() => (paid ? setConfirm(true) : retry.mutate())}>
                <RotateCw /> {retry.isPending ? 'Retrying…' : 'Retry blocked cells'}
              </Button>
            </div>
          )}
          {retry.error && <Alert>{retry.error.message}</Alert>}
          {confirm && <RetryDialog x={x} blocked={blocked} busy={retry.isPending} onRetry={() => retry.mutate()} onClose={() => setConfirm(false)} />}
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-muted-foreground">Id</dt>
            <dd>
              <Mono>{x.id}</Mono>
            </dd>
            <dt className="text-muted-foreground">Request hash</dt>
            <dd>
              <Mono>{x.hash}</Mono>
            </dd>
            <dt className="text-muted-foreground">Created</dt>
            <dd>{new Date(x.created_at).toLocaleString()}</dd>
            <dt className="text-muted-foreground">Split</dt>
            <dd>{x.split}</dd>
            <dt className="text-muted-foreground">Cells</dt>
            <dd>{countsText(x.counts)}</dd>
            <dt className="text-muted-foreground">Spend</dt>
            <dd>
              {spendText(x.spend, x.batch_budget)}
            </dd>
            <dt className="text-muted-foreground">Branch plan</dt>
            <dd>
              {[
                x.branch_plan.probe && `probe on ${x.branch_plan.probe.target} at round ${x.branch_plan.probe.at} for ${x.branch_plan.probe.rounds} rounds`,
                x.branch_plan.placebo && `${x.branch_plan.placebo.n} placebo at round ${x.branch_plan.placebo.at} for ${x.branch_plan.placebo.rounds} rounds`,
                x.branch_plan.ablation && `ablation (${x.branch_plan.ablation.mode}) from round ${x.branch_plan.ablation.at} for ${x.branch_plan.ablation.groups?.join(', ')}`,
              ]
                .filter(Boolean)
                .join('; ') || 'no children'}
            </dd>
          </dl>
          <Grid x={x} />
          <details className="rounded-lg border p-3">
            <summary className="cursor-pointer text-sm font-medium">Estimate at the start</summary>
            <div className="mt-2">
              <EstimateView e={x.estimate} />
            </div>
          </details>
        </>
      )}
    </div>
  )
}
