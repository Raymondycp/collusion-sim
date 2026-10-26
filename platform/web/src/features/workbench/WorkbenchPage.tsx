import { useMemo } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useRun } from '@/api/queries'
import { ApiError } from '@/api/client'
import { PriceChart } from '@/components/chart/PriceChart'
import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { firstT, lastT, viewAt } from '@/run/fold'
import { useRunStream } from '@/run/useRunStream'
import { StatusBadge } from '@/features/runs/badges'
import { TimelineBar } from '@/features/run-view/TimelineBar'
import { useViewParams } from '@/features/run-view/viewParams'
import { AgentCard, EventLog, HostCard, PublicChannel, SettingSummary } from './Panels'
import { TrailDrawer } from './TrailDrawer'
import { MetricsBar } from './MetricsBar'
import { fallbackWarning, RunHealth } from './RunHealth'
import { runHealth } from './health'
import { RivalChart } from './RivalChart'
import { finalRound, panelRound, publicMarkers } from './derive'
import { conditionText } from './labels'
import { PHASE_TEXT } from '@/run/phase'
import { RunControls } from '@/features/run-view/RunControls'
import { RUN_EXPORTS } from '@/api/exports'
import { ENDED } from '@/api/experiments'
import { ExportMenu } from '@/features/export/ExportMenu'

/**
 * Run workbench, replay and live mode (docs/sim/UI-WORKBENCH.md). Every panel follows the scrubber round in `?t=`.
 * `?experiment=` and `?evaluation=` link the run to its feature-table row, for the judge and branch metrics. Without
 * `?experiment=`, the run's own experiment_id links it.
 * `?view=public` hides the secret-channel inboxes. `?call=<seq>` opens the trail of that llm_call, in god view only: a trail
 * holds secret-channel text and restricted news.
 */
export function WorkbenchPage({ onClose }: { onClose?: () => void } = {}) {
  const runId = useParams().runId!
  const run = useRun(runId)
  const stream = useRunStream(runId)
  const { t, setT } = useViewParams()
  const [params, setParams] = useSearchParams()
  const publicView = params.get('view') === 'public'
  const experimentId = params.get('experiment') ?? run.data?.experiment_id ?? null
  const ablationOf = run.data?.condition_id == null && run.data?.branch?.intervention.type === 'ablation' ? run.data.branch.parent_run_id : null
  const parent = useRun(ablationOf ?? '', { enabled: !!ablationOf })
  const evaluationId = params.get('evaluation')
  const callParam = params.get('call')
  const call = !publicView && callParam !== null && /^\d+$/.test(callParam) ? Number(callParam) : null
  const setCall = (seq: number | null) =>
    setParams(
      (p) => {
        if (seq === null) p.delete('call')
        else p.set('call', String(seq))
        return p
      },
      { replace: true },
    )
  const onTrail = publicView ? undefined : setCall
  // The calls of the run in order, for Previous and Next in the drawer. Only the god view opens the drawer.
  const callSeqs = useMemo(() => (publicView ? [] : stream.state.events.flatMap((e) => (e.type === 'llm_call' ? [e.seq] : [])).sort((a, b) => a - b)), [publicView, stream.state.events])

  const full = stream.state
  const minT = firstT(full)
  const maxT = lastT(full)
  const cursor = t === null ? maxT : Math.min(t, maxT)
  const view = useMemo(() => viewAt(full, t === null ? null : cursor), [full, t, cursor])
  const roundT = panelRound(full, t)
  const shown = useMemo(
    () => (publicView ? { view: { ...view, markers: publicMarkers(full, view.markers) }, markers: publicMarkers(full, full.markers) } : { view, markers: full.markers }),
    [publicView, view, full],
  )
  const sellers = (full.header?.agents ?? []).filter((a) => a.role === 'seller').map((a) => a.id)
  const status = full.status ?? run.data?.status
  const ended = !!status && ENDED.includes(status)
  const health = useMemo(() => runHealth(full), [full])

  if (stream.phase === 'not_found' || (run.error instanceof ApiError && run.error.code === 'not_found')) {
    return (
      <div className="py-16 text-center">
        <p className="text-lg font-medium">Run {runId} was not found.</p>
        <div className="mt-2 flex items-center justify-center gap-3">
          {onClose && (
            <Button type="button" variant="outline" size="sm" onClick={onClose}>
              <X /> Close
            </Button>
          )}
          <Link to="/runs" className="text-sm underline underline-offset-4">
            Go to the run list
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h1 className="truncate font-mono text-lg font-semibold">{runId}</h1>
          {status && <StatusBadge status={status} />}
          <span className="text-xs text-muted-foreground" aria-live="polite">
            {PHASE_TEXT[stream.phase]}
            {stream.detail ? `: ${stream.detail}` : ''}
            {t === null && roundT > cursor ? (ended ? `, ended during R${roundT}; last settled round R${cursor}` : `, round ${roundT} in progress`) : ''}
          </span>
          <RunControls runId={runId} status={status} split={run.data ? (run.data.split ?? null) : undefined} />
          <ExportMenu id={runId} items={RUN_EXPORTS} live={!!status && !ENDED.includes(status)} publicView={publicView} />
        </div>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          aria-label="View"
          value={publicView ? 'public' : 'god'}
          onValueChange={(v) =>
            v &&
            setParams(
              (p) => {
                if (v === 'public') p.set('view', 'public')
                else p.delete('view')
                return p
              },
              { replace: true },
            )
          }>
          <ToggleGroupItem value="god">God view</ToggleGroupItem>
          <ToggleGroupItem value="public">Public view</ToggleGroupItem>
        </ToggleGroup>
      </header>
      <SettingSummary run={run.data} condition={run.data ? conditionText(run.data, parent.data) : null} state={full} />

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-3">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {sellers.map((id) => (
              <AgentCard key={id} state={view} agentId={id} cursorT={roundT} publicView={publicView} onTrail={onTrail} final={finalRound(full, status)} />
            ))}
            <HostCard state={view} cursorT={roundT} publicView={publicView} />
          </div>
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_20rem]">
            <div className="min-w-0 space-y-2 rounded-lg border p-2">
              <PriceChart state={shown.view} overlays={[]} onPickT={setT} />
              <TimelineBar minT={minT} maxT={maxT} t={t} onChange={setT} markers={shown.markers} following={t === null} />
            </div>
            <RivalChart state={full} cursorT={cursor} />
          </div>
          <RunHealth state={full} publicView={publicView} errorDetail={run.data?.error_detail} />
          <MetricsBar caution={publicView ? null : fallbackWarning(health)} publicView={publicView} split={run.data ? (run.data.split ?? null) : undefined} runId={runId} stream={stream} status={run.data?.status} cursorT={cursor} experimentId={experimentId} evaluationId={evaluationId} />
          <PublicChannel state={view} cursorT={roundT} />
        </div>
        <aside className="min-w-0">
          <EventLog state={view} cursorT={roundT} onPickT={setT} publicView={publicView} onTrail={onTrail} />
        </aside>
      </div>
      <TrailDrawer runId={runId} seq={call} seqs={callSeqs} onSelect={setCall} onClose={() => setCall(null)} />
    </div>
  )
}
