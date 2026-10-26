import type { RunState } from '@/run/fold'
import { cn } from '@/lib/utils'
import type { components } from '@/api/schema'
import { END_TEXT, ERROR_TEXT, errorDetailLines, errorDetailOf, runHealth, type RunHealth as Health } from './health'

const pct = (a: number, b: number) => (b === 0 ? '0%' : `${Math.round((a / b) * 100)}%`)
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`

/** The kind, the provider's text and the place of a failure (contract 0.38.0 `error_detail`). Shared by the workbench and the run page. */
export function ErrorDetailList({ detail }: { detail: NonNullable<components['schemas']['ErrorDetail']> }) {
  return (
    <ul aria-label="Error detail" className="list-disc space-y-0.5 pl-4 text-xs">
      {errorDetailLines(detail).map((l) => (
        <li key={l.key}>{l.text}</li>
      ))}
    </ul>
  )
}

/** The warning that goes with the metrics when most prices are fallbacks (UX review 2, item 1). */
export function fallbackWarning(h: Health): string | null {
  return h.mostFallback
    ? `${h.fallbacks} of ${plural(h.decisions, 'seller decision')} (${pct(h.fallbacks, h.decisions)}) kept the last price as a fallback. These metrics describe the fallback rule more than the model.`
    : null
}

/**
 * What went wrong in the run, above the metrics (UX review 2, item 1): failed calls, retries, fallback prices with their cause, and
 * how the run ended. God view only, because the public view has no model calls.
 */
export function RunHealth({ state, publicView, errorDetail }: { state: RunState; publicView: boolean; errorDetail?: components['schemas']['ErrorDetail'] | null }) {
  if (publicView) return null
  const h = runHealth(state)
  const ended = state.status !== null && ['completed', 'stopped', 'failed'].includes(state.status)
  const clean = h.failedTotal === 0 && h.retries === 0 && h.fallbacks === 0 && h.degraded === 0 && h.timeoutFallbacks === 0
  const end = ended ? `${state.status}${state.endReason ? `: ${END_TEXT[state.endReason] ?? state.endReason}` : ''}${state.statusReason ? ` (${state.statusReason})` : ''}` : null
  const warn = fallbackWarning(h)
  const detail = errorDetailOf(state, errorDetail)
  return (
    <section aria-label="Run health" className={cn('space-y-1.5 rounded-lg border p-3 text-sm', warn && 'border-amber-500/60')}>
      <h3 className="font-medium">Run health</h3>
      {warn && (
        <p role="status" className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
          {warn}
        </p>
      )}
      {clean ? (
        <p className="text-xs text-muted-foreground">{h.calls === 0 ? 'No model calls in this run.' : `${plural(h.calls, 'model call')}, none failed, no retries and no fallback prices.`}</p>
      ) : (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">Failed calls</dt>
          <dd>
            {h.failedTotal} of {plural(h.calls, 'call')} ({pct(h.failedTotal, h.calls)})
            {h.failedTotal > 0 && (
              <ul aria-label="Failed calls by error" className="mt-0.5 list-disc pl-4 text-muted-foreground">
                {Object.entries(h.failed)
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, n]) => (
                    <li key={k}>
                      {n} × {k}: {ERROR_TEXT[k] ?? 'other'}
                    </li>
                  ))}
              </ul>
            )}
          </dd>
          <dt className="text-muted-foreground">Retries</dt>
          <dd>{h.retries} (calls with attempt 2 or more)</dd>
          {h.degraded > 0 && (
            <>
              <dt className="text-muted-foreground">Degraded calls</dt>
              <dd>{h.degraded} (the retry after a slow attempt sent lower thinking, so these replies are not like the others)</dd>
            </>
          )}
          {h.timeoutFallbacks > 0 && (
            <>
              <dt className="text-muted-foreground">Timeout fallbacks</dt>
              <dd className="font-medium text-red-600 dark:text-red-400">{h.timeoutFallbacks}: the system held or passed after two slow attempts. This fails the manipulation check of the run (feature timeout_fallbacks).</dd>
            </>
          )}
          <dt className="text-muted-foreground">Fallback prices</dt>
          <dd>
            {h.fallbacks} of {plural(h.decisions, 'seller decision')} ({pct(h.fallbacks, h.decisions)})
            {h.allFallbackRounds.length > 0 && <>, every seller fell back in {plural(h.allFallbackRounds.length, 'round')} of {h.rounds}</>}
            {h.fallbackWhy.length > 0 && (
              <ul aria-label="Fallback prices by reason" className="mt-0.5 list-disc pl-4 text-muted-foreground">
                {h.fallbackWhy.map((w) => (
                  <li key={`${w.inferred}:${w.reason}`}>
                    {w.count} × {w.reason}
                    {w.inferred ? ' (from the last failed call of that seller in that round)' : ''}
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </dl>
      )}
      {end && (
        <p className="text-xs">
          <span className="text-muted-foreground">End: </span>
          {end}
        </p>
      )}
      {detail && <ErrorDetailList detail={detail} />}
    </section>
  )
}
