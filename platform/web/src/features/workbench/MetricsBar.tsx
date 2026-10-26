import { useMemo } from 'react'
import { cutAt, defaultParams, useIndicatorSpecs, type IndicatorSelection } from '@/api/indicators'
import { useBranches } from '@/api/queries'
import type { RunStatus } from '@/api/types'
import { useRunIndicators } from '@/run/useRunIndicators'
import type { RunStream } from '@/run/useRunStream'
import { cn } from '@/lib/utils'
import { INDICATOR_IDS, METRIC_GROUPS, useFeatureRow, type Metric } from './metrics'

/** Which intervention type gives each branch feature its value. */
const NEEDS_BRANCH: Record<string, 'ablation' | 'probe'> = { ablation_effect: 'ablation', punishment_depth: 'probe' }

/**
 * One value at the cursor from an indicator's outputs. The output named like the indicator is the all-seller value.
 * Without it (corr_* per pair, br_gap_net per seller), combine the outputs as the registry feature does:
 * the largest over ordered pairs for corr_lag, else the mean.
 */
export function atCursor(id: string, outputs: Record<string, (number | null)[]>): number | null {
  const lastOf = (vs: (number | null)[]) => [...vs].reverse().find((v) => v !== null) ?? null
  if (outputs[id]) return lastOf(outputs[id])
  const vals = Object.values(outputs).flatMap((vs) => {
    const v = lastOf(vs)
    return v === null ? [] : [v]
  })
  if (!vals.length) return null
  return id === 'corr_lag' ? Math.max(...vals) : vals.reduce((a, b) => a + b, 0) / vals.length
}

const fmt = (v: number) => (Number.isInteger(v) ? String(v) : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(3))

type Props = {
  runId: string
  stream: RunStream
  status: RunStatus | null | undefined
  cursorT: number
  experimentId: string | null
  evaluationId: string | null
  publicView: boolean
  /** undefined: the run record has not loaded, so the split is not known. null: the run has no split. */
  split: string | null | undefined
  /** Set when most prices of the run are fallbacks (RunHealth): the metrics then carry a caution. */
  caution?: string | null
}

const ENDED = new Set<RunStatus>(['completed', 'stopped', 'failed'])
/** Dev and test runs show no metric while live, so nobody stops a run on its value (UI-WORKBENCH.md §3, CR-49). */
const LOCKED_SPLITS = new Set(['dev', 'test'])

/** Public view greys this group: message counts and judge scores come from the private talk (UI-WORKBENCH.md §2). */
const PRIVATE_GROUP = 'Conversation'

/**
 * Three evidence groups, the same for every run (UI-WORKBENCH.md §5). Indicator metrics show their value at the cursor round.
 * Feature metrics have only a final value, from the experiment feature table. No combined score.
 */
export function MetricsBar({ runId, stream, status, cursorT, experimentId, evaluationId, publicView, split, caution = null }: Props) {
  const ended = [stream.state.status, status].some((s) => s != null && ENDED.has(s))
  // An unknown split fails closed: a dev or test run must not show a value before its record loads (CR-54 2).
  const locked = !ended && (split === undefined || (split !== null && LOCKED_SPLITS.has(split)))
  const specs = useIndicatorSpecs()
  const selection: IndicatorSelection[] = useMemo(
    () => (locked ? [] : (specs.data ?? []).filter((s) => INDICATOR_IDS.includes(s.id)).map((s) => ({ id: s.id, params: defaultParams(s) }))),
    [specs.data, locked],
  )
  const indicators = useRunIndicators([{ id: runId, stream, status }], selection)
  const features = useFeatureRow(runId, locked ? null : experimentId, evaluationId)
  const branches = useBranches(runId)
  const branchTypes = new Set<string>((branches.data ?? []).flatMap((b) => (b.branch ? [b.branch.intervention.type] : [])))

  const valueOf = (m: Metric): { text: string; muted: boolean } => {
    if (m.source === 'indicator') {
      if (specs.data && !specs.data.some((s) => s.id === m.id)) return { text: 'not on this server', muted: true }
      const data = indicators.data.find((d) => d.indicator_id === m.id)
      if (!data) return { text: '–', muted: true }
      const last = atCursor(m.id, cutAt(data, cursorT).outputs)
      return last === null ? { text: 'no value yet', muted: true } : { text: fmt(last), muted: false }
    }
    const cell = features.data?.[m.id]
    if (cell?.value != null) return { text: fmt(cell.value), muted: false }
    if (cell?.missing) return { text: cell.missing.replaceAll('_', ' '), muted: true }
    if (NEEDS_BRANCH[m.id] && !branchTypes.has(NEEDS_BRANCH[m.id]!)) return { text: 'no branch', muted: true }
    if (m.tag === 'needs a judge job' && !evaluationId) return { text: 'not judged', muted: true }
    return { text: experimentId ? 'not in the export' : 'no experiment', muted: true }
  }

  return (
    <section aria-label="Metrics" className={cn('grid gap-3 rounded-lg border p-3 md:grid-cols-3', caution && 'border-amber-500/60')}>
      {caution && (
        <p aria-label="Metrics caution" className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs md:col-span-3">
          Caution: {caution}
        </p>
      )}
      {METRIC_GROUPS.map((g) => {
        const hide = publicView && g.name === PRIVATE_GROUP
        const rows = g.metrics.map((m) => ({ m, v: hide ? { text: 'hidden in public view', muted: true } : locked ? { text: 'after the run ends', muted: true } : valueOf(m) }))
        const measured = rows.filter((r) => !r.v.muted)
        const missing = rows.filter((r) => r.v.muted)
        return (
          <div key={g.name} role="group" aria-label={g.name} className={cn('min-w-0 space-y-1.5', hide && 'opacity-50')}>
            <h3 className="text-sm font-medium">{g.name}</h3>
            {measured.length > 0 && (
              <dl aria-label={`${g.name}: measured`} className="space-y-1 text-xs">
                {measured.map(({ m, v }) => (
                  <div key={m.id} className="flex items-baseline justify-between gap-2" title={m.definition}>
                    <dt className="min-w-0">
                      <span>{m.label}</span> <span className="rounded border px-1 text-[10px] text-muted-foreground">{m.tag}</span>
                    </dt>
                    <dd data-metric={m.id} className="shrink-0 font-medium tabular-nums">
                      {v.text}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            {/* A placeholder is never a measurement: the value is a dash with its reason, under its own heading (UX review item 8). */}
            {(missing.length > 0 || g.designed.length > 0) && (
              <div className="space-y-1">
                <p className="text-[10px] tracking-wide text-muted-foreground uppercase">Not available</p>
                <dl aria-label={`${g.name}: not available`} className="space-y-1 text-xs text-muted-foreground">
                  {missing.map(({ m, v }) => (
                    <div key={m.id} className="flex items-baseline justify-between gap-2" title={m.definition}>
                      <dt className="min-w-0">
                        <span>{m.label}</span> <span className="rounded border px-1 text-[10px]">{m.tag}</span>
                      </dt>
                      <dd data-metric={m.id} data-unavailable="true" className="shrink-0 tabular-nums before:mr-1 before:content-['—']">
                        {v.text}
                      </dd>
                    </div>
                  ))}
                  {g.designed.map((d) => (
                    <div key={d.label} className="flex items-baseline justify-between gap-2 opacity-60" title={d.definition}>
                      <dt>{d.label}</dt>
                      <dd className="shrink-0 italic before:mr-1 before:content-['—']">designed, not built</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}
          </div>
        )
      })}
      <p className="text-xs text-muted-foreground md:col-span-3">
        {locked ? (split === undefined ? 'The split of this run is not known yet: no metrics until the run record loads. ' : `A ${split} run shows its metrics only after it ends. `) : ''}
        Indicator values are at R{cursorT}. Judge and branch values are final values from the experiment feature table
        {experimentId ? '' : ' (this run is not in an experiment)'}. {indicators.error ? `Indicator error: ${indicators.error}` : ''}
      </p>
    </section>
  )
}
