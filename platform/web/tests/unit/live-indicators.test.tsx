import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { setupServer } from 'msw/node'
import { RunSocket, type SocketPhase } from '@/api/socket'
import type { IndicatorUpdate, RunStatus } from '@/api/types'
import { handlers, wsHandlers } from '@/mocks/handlers'
import { addMockRun } from '@/mocks/data'
import { makeEpisode } from '@/mocks/episode'
import { applyEvents, emptyRunState, foldEvents, lastT, viewAt } from '@/run/fold'
import { indicatorMode, useRunIndicators } from '@/run/useRunIndicators'
import { storeFor, type RunStream } from '@/run/useRunStream'

const requests: string[] = []
const server = setupServer(...handlers, ...wsHandlers)
server.events.on('request:start', ({ request }) => void requests.push(request.url))
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => void (requests.length = 0))
afterAll(() => server.close())

const stream = (status: RunStatus | null, phase: SocketPhase): RunStream => ({ state: { ...emptyRunState, status }, phase, detail: null, indicators: {}, indicatorError: null })

describe('live or REST (lead triage Q9, CR-2 m14)', () => {
  it.each([
    ['REST says completed, replay not done yet', null, 'replaying', 'completed', 'done'],
    ['the stream folded run_ended', 'completed', 'ended', 'running', 'done'],
    ['REST says running', null, 'connecting', 'running', 'live'],
    ['no REST status, socket caught up with no end', 'running', 'live', undefined, 'live'],
    ['no REST status, replay in progress', null, 'replaying', undefined, 'unknown'],
  ] as const)('%s: %s', (_, folded, phase, rest, mode) => {
    expect(indicatorMode(stream(folded, phase), rest)).toBe(mode)
  })

  it('a first load of a finished run makes one REST call for all rounds and subscribes nothing', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const selection = [{ id: 'delta_p', params: { smooth: 1 } }]
    const { result } = renderHook(() => useRunIndicators([{ id: 'run_MOCK01', stream: stream(null, 'replaying'), status: 'completed' }], selection), { wrapper })
    await waitFor(() => expect(result.current.data).toHaveLength(1))
    const calls = requests.filter((u) => u.includes('/indicators/delta_p'))
    expect(calls).toHaveLength(1)
    expect(new URL(calls[0]!).searchParams.has('until')).toBe(false)
    expect(result.current.data[0]!.t).toHaveLength(30)
    expect(storeFor('run_MOCK01').getSnapshot().indicators).toEqual({})
  })
})

describe('mock live run (CR-2 M5)', () => {
  it('sends a non-null Δp for each new round, after the round has its reference values', async () => {
    const runId = addMockRun({ runId: 'run_LIVE_M5', kind: 'exploratory', seed: 3, rounds: 3, start: new Date().toISOString(), conditionId: null, branch: null })
    const updates: IndicatorUpdate[] = []
    let phase: SocketPhase = 'connecting'
    const socket = new RunSocket(runId, 0, { onEvents: () => {}, onPhase: (p) => (phase = p), onIndicator: (u) => updates.push(u) })
    socket.setSubscriptions([{ key: 'k', id: 'delta_p', params: {} }])
    await waitFor(() => expect(phase).toBe('ended'), { timeout: 15_000 })
    socket.close()
    expect(updates.map((u) => u.t)).toEqual([1, 2, 3])
    for (const u of updates) expect(u.values.delta_p).not.toBeNull()
  }, 20_000)
})

describe('rounds run (CR-2 M6, lead triage Q10)', () => {
  const events = makeEpisode({ runId: 'run_Q10', kind: 'exploratory', seed: 1, rounds: 5, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null })
  const startOf3 = events.findIndex((e) => e.type === 'round_started' && e.t === 3)

  it('while round 3 is in progress, the last settled round is 2', () => {
    const s = applyEvents(emptyRunState, events.slice(0, startOf3 + 2))
    expect(s.t).toBe(3)
    expect(s.settledT).toBe(2)
    expect(lastT(s)).toBe(2)
  })

  it('a cut at an earlier round has that round as its last settled round', () => {
    expect(viewAt(foldEvents(events), 2).settledT).toBe(2)
  })
})

describe('mock branches (CR-2 M2)', () => {
  it('numbers placebo children 1..n, because the parent itself is draw 0', async () => {
    const url = (p: string) => new URL(p, location.href)
    const res = await fetch(url('/api/runs/run_MOCK01/branches'), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ at: 10, rounds: 2, n: 3, intervention: { type: 'placebo' } }) })
    const { run_ids } = (await res.json()) as { run_ids: string[] }
    const indexes = await Promise.all(run_ids.map(async (id) => ((await (await fetch(url(`/api/runs/${id}`))).json()) as { branch: { sample_index: number } }).branch.sample_index))
    expect(indexes).toEqual([1, 2, 3])
  })
})
