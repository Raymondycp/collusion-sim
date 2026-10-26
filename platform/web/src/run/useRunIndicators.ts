import { useEffect } from 'react'
import { keepPreviousData, useQueries } from '@tanstack/react-query'
import { api, unwrap } from '@/api/client'
import { instanceKey, type IndicatorData, type IndicatorSelection } from '@/api/indicators'
import type { RunStatus } from '@/api/types'
import { storeFor, type RunStream } from './useRunStream'

const FINISHED = new Set<RunStatus>(['completed', 'stopped', 'failed'])
const ended = (s: RunStatus | null | undefined) => s != null && FINISHED.has(s)

type Mode = 'done' | 'live' | 'unknown'

/**
 * Finished if either source says so: the REST summary knows it before the socket replay reaches `run_ended`.
 * Live if REST says so, or if the socket caught up with no `run_ended` (a finished run sends it before `caught_up`).
 * Otherwise unknown, and nothing is fetched.
 */
export function indicatorMode(stream: RunStream, status: RunStatus | null | undefined): Mode {
  if (ended(stream.state.status) || ended(status)) return 'done'
  return status || stream.phase === 'live' ? 'live' : 'unknown'
}

/**
 * The selected indicators for each run (lead triage Q9). A finished run reads each instance once over REST.
 * A live run subscribes on its WebSocket and gets one update per round, after the round's `reference` event.
 * `status` is the run status from REST. Returns one entry per run and indicator that has data, in run order.
 */
export function useRunIndicators(
  runs: { id: string; stream: RunStream; status: RunStatus | null | undefined }[],
  selection: IndicatorSelection[],
): { data: IndicatorData[]; error: string | null } {
  const modes = runs.map((r) => indicatorMode(r.stream, r.status))
  const finished = modes.map((m) => m === 'done')
  const subsKey = JSON.stringify(selection.map((s) => [instanceKey(s), s.id, s.params]))
  const liveKey = runs.map((r, i) => `${r.id}:${modes[i]}`).join(',')

  useEffect(() => {
    const subs = (JSON.parse(subsKey) as [string, string, Record<string, unknown>][]).map(([key, id, params]) => ({ key, id, params }))
    const list = liveKey ? liveKey.split(',').map((x) => x.split(':') as [string, string]) : []
    for (const [id, mode] of list) storeFor(id).setIndicators(mode === 'live' ? subs : [])
    return () => {
      for (const [id] of list) storeFor(id).setIndicators([])
    }
  }, [subsKey, liveKey])

  const rest = useQueries({
    queries: runs.flatMap((r, i) =>
      // A finished run has all its rounds, so the request leaves out `until` and does not wait for the replay.
      selection.map((s) => ({
        queryKey: ['indicator', r.id, s.id, s.params],
        enabled: finished[i]!,
        queryFn: () =>
          unwrap(
            api.GET('/runs/{run_id}/indicators/{indicator_id}', {
              params: { path: { run_id: r.id, indicator_id: s.id }, query: { params: JSON.stringify(s.params) } },
            }),
          ),
        placeholderData: keepPreviousData,
        staleTime: Infinity,
      })),
    ),
  })

  const data: IndicatorData[] = []
  let error: string | null = null
  runs.forEach((r, i) => {
    selection.forEach((s, j) => {
      if (finished[i]) {
        const q = rest[i * selection.length + j]
        if (q?.error) error ??= q.error.message
        if (q?.data) data.push(q.data)
      } else {
        const live = r.stream.indicators[instanceKey(s)]
        if (live) data.push({ run_id: r.id, ...live })
      }
    })
    error ??= r.stream.indicatorError
  })
  return { data, error }
}
