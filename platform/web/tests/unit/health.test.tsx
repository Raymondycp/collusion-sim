import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { handlers, wsHandlers } from '@/mocks/handlers'
import { makeEpisode } from '@/mocks/episode'
import { mockRuns } from '@/mocks/data'
import type { SimEvent } from '@/api/types'
import { emptyRunState, foldEvents, type RunState } from '@/run/fold'
import { runHealth } from '@/features/workbench/health'
import { RunHealth } from '@/features/workbench/RunHealth'
import { MetricsBar } from '@/features/workbench/MetricsBar'
import { WorkbenchPage } from '@/features/workbench/WorkbenchPage'
import { AgentCard } from '@/features/workbench/Panels'

vi.mock('lightweight-charts', () => {
  const any: unknown = new Proxy(() => any, { get: (_, k) => (k === 'then' ? undefined : any), apply: () => any })
  return { ColorType: {}, LineStyle: {}, LineType: {}, LineSeries: 'Line', HistogramSeries: 'Histogram', createSeriesMarkers: () => any, createChart: () => any }
})
vi.mock('@/components/chart/PriceChart', async (orig) => ({ ...(await orig<typeof import('@/components/chart/PriceChart')>()), PriceChart: () => null }))
vi.mock('@/features/run-view/TimelineBar', () => ({ TimelineBar: () => null }))

const server = setupServer(...handlers, ...wsHandlers)
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
})

const call = (seq: number, t: number, agent: string, over: Record<string, unknown> = {}) =>
  ({ seq, t, run_id: 'r', wall_time: 'w', tier: 'B', type: 'llm_call', agent_id: agent, purpose: 'act', attempt: 1, cache_hit: false, error: null, ...over }) as unknown as SimEvent
const acted = (seq: number, t: number, agent: string, over: Record<string, unknown> = {}) =>
  ({ seq, t, run_id: 'r', wall_time: 'w', tier: 'B', type: 'acted', agent_id: agent, price: 5, hold: false, fallback: false, forced: false, ...over }) as unknown as SimEvent
const state = (events: SimEvent[], extra: Partial<RunState> = {}): RunState => ({ ...emptyRunState, events, ...extra })

describe('error detail on the End line (contract 0.38.0, UX review 2 item 2)', () => {
  const failed = { ...emptyRunState, status: 'failed' as const, endReason: 'error' as const, statusReason: 'auth' }
  const detail = { code: 'auth', exception: 'EndpointError', http_status: 401, provider_code: 'invalid_api_key', provider_message: 'Incorrect key [redacted]', message: 'EndpointError: 401', where: { phase: 'act' as const, seller_id: 'A', t: 5 } }

  it('shows the kind, HTTP status, provider code and message, and where it happened', () => {
    render(<RunHealth state={failed} publicView={false} errorDetail={detail} />)
    const list = screen.getByRole('list', { name: 'Error detail' })
    expect(within(list).getAllByRole('listitem').map((x) => x.textContent)).toEqual([
      'auth (EndpointError) HTTP 401 provider code invalid_api_key',
      'Provider message: Incorrect key [redacted]',
      'During act of seller A in round 5.',
    ])
  })

  it('without a provider message it shows the exception message; a failure outside a seller call says so', () => {
    render(<RunHealth state={failed} publicView={false} errorDetail={{ code: 'KeyError', exception: 'KeyError', message: "KeyError: 'x'" }} />)
    expect(screen.getByRole('list', { name: 'Error detail' })).toHaveTextContent("KeyError: 'x'Outside a seller call.")
    expect(screen.getByRole('list', { name: 'Error detail' })).not.toHaveTextContent(/HTTP|provider code/)
  })

  it('a live stream that holds run_ended.error_detail shows it with no RunDetail', () => {
    const ev = { seq: 9, t: 5, run_id: 'r', wall_time: 'w', tier: 'B', type: 'run_ended', status: 'failed', end_reason: 'error', error: 'auth', error_detail: detail } as unknown as SimEvent
    render(<RunHealth state={{ ...failed, events: [ev] }} publicView={false} />)
    expect(screen.getByRole('list', { name: 'Error detail' })).toHaveTextContent('HTTP 401')
  })

  it('two equal lines do not collide: the list keys are the line names, not the text', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<RunHealth state={failed} publicView={false} errorDetail={{ code: 'Outside a seller call.', exception: 'Outside a seller call.', where: null }} />)
    expect(spy.mock.calls.filter((c) => String(c[0]).includes('same key'))).toEqual([])
    spy.mockRestore()
  })

  it('a run with no error detail shows no list, and the public view shows no card', () => {
    const { unmount } = render(<RunHealth state={failed} publicView={false} errorDetail={null} />)
    expect(screen.queryByRole('list', { name: 'Error detail' })).toBeNull()
    unmount()
    render(<RunHealth state={failed} publicView errorDetail={detail} />)
    expect(screen.queryByRole('region', { name: 'Run health' })).toBeNull()
  })
})

describe('run health from the events (UX review 2, item 1)', () => {
  it('counts failed calls by error, retries, and fallback decisions with a cause from the last failed call of that seller', () => {
    const h = runHealth(
      state([
        call(1, 1, 'A', { error: 'timeout' }),
        call(2, 1, 'A', { attempt: 2, error: 'timeout' }),
        acted(3, 1, 'A', { fallback: true }),
        call(4, 1, 'B'),
        acted(5, 1, 'B'),
        call(6, 2, 'A', { error: 'rate_limit' }),
        acted(7, 2, 'A', { fallback: true }),
        acted(8, 2, 'B', { fallback: true }),
      ]),
    )
    expect(h).toMatchObject({ calls: 4, failedTotal: 3, failed: { timeout: 2, rate_limit: 1 }, retries: 1, decisions: 4, fallbacks: 3, rounds: 2, allFallbackRounds: [2] })
    expect(h.fallbackWhy).toHaveLength(3)
    expect(h.fallbackWhy).toEqual(
      expect.arrayContaining([
        { reason: 'the call timed out', inferred: true, count: 1 },
        { reason: 'the provider limited the rate', inferred: true, count: 1 },
        { reason: 'no failed call is recorded for that turn', inferred: true, count: 1 },
      ]),
    )
    expect(h.mostFallback).toBe(true)
  })

  it('a reason that the record names (fallback_reason, contract 0.35.0) wins and is not marked as inferred', () => {
    const h = runHealth(state([call(1, 1, 'A', { error: 'provider_error' }), acted(2, 1, 'A', { fallback: true, fallback_reason: 'timeout' }), acted(3, 1, 'B', { fallback: true, fallback_reason: 'max_steps' })]))
    expect(h.fallbackWhy).toEqual(
      expect.arrayContaining([
        { reason: 'two slow attempts (timeout)', inferred: false, count: 1 },
        { reason: 'the steps ran out', inferred: false, count: 1 },
      ]),
    )
    expect(h.fallbackWhy).toHaveLength(2)
  })

  it('counts degraded calls, and timeout fallbacks from acted and from a timed-out PASS in the talk', () => {
    const talk = (seq: number, t: number, reason: string | null) => ({ seq, t, run_id: 'r', wall_time: 'w', tier: 'B', type: 'talk_replied', agent_id: 'A', outcome: 'pass', reason }) as unknown as SimEvent
    const h = runHealth(state([call(1, 1, 'A', { degraded: true }), call(2, 1, 'B', { degraded: false }), call(3, 2, 'A', { degraded: true }), talk(4, 1, 'timeout'), talk(5, 2, 'error'), talk(6, 2, null), acted(7, 2, 'A', { fallback: true, fallback_reason: 'timeout' }), acted(8, 2, 'B', { fallback: true, fallback_reason: 'error' })]))
    expect(h).toMatchObject({ degraded: 2, timeoutFallbacks: 2 })
  })

  it('fewer than half fallbacks is not "most"; no decisions is not "most"', () => {
    expect(runHealth(state([acted(1, 1, 'A', { fallback: true }), acted(2, 1, 'B'), acted(3, 2, 'A'), acted(4, 2, 'B')])).mostFallback).toBe(false)
    expect(runHealth(state([])).mostFallback).toBe(false)
    expect(runHealth(state([acted(1, 1, 'A', { fallback: true }), acted(2, 1, 'B'), acted(3, 2, 'A', { fallback: true }), acted(4, 2, 'B'), acted(5, 3, 'A')])).mostFallback).toBe(false)
    expect(runHealth(state([acted(1, 1, 'A', { fallback: true }), acted(2, 1, 'B')])).mostFallback).toBe(true)
  })
})

describe('the run-health card', () => {
  it('a clean run says so in one line, with the end reason of a finished run', () => {
    const s = foldEvents(makeEpisode({ runId: 'r', kind: 'exploratory', seed: 1, rounds: 6, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null }))
    render(<RunHealth state={{ ...s, status: 'completed', endReason: 'horizon' }} publicView={false} />)
    const card = screen.getByRole('region', { name: 'Run health' })
    expect(card).toHaveTextContent(/none failed, no retries and no fallback prices/)
    expect(card).toHaveTextContent('End: completed: the run reached its last round')
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('a bad run lists the failures, the fallbacks with their cause, a warning, and how it ended', () => {
    const s = state(
      [call(1, 1, 'A', { error: 'provider_error' }), acted(2, 1, 'A', { fallback: true }), acted(3, 1, 'B', { fallback: true }), call(4, 2, 'A'), acted(5, 2, 'A'), acted(6, 2, 'B', { fallback: true })],
      { status: 'stopped', endReason: 'budget_exceeded' },
    )
    render(<RunHealth state={s} publicView={false} />)
    const card = screen.getByRole('region', { name: 'Run health' })
    expect(within(card).getByRole('status')).toHaveTextContent('3 of 4 seller decisions (75%) kept the last price as a fallback')
    expect(within(card).getByRole('list', { name: 'Failed calls by error' })).toHaveTextContent('1 × provider_error: the provider failed')
    expect(within(card).getByRole('list', { name: 'Fallback prices by reason' })).toHaveTextContent('1 × the provider failed (from the last failed call of that seller in that round)')
    expect(card).toHaveTextContent('every seller fell back in 1 round of 2')
    expect(card).toHaveTextContent('End: stopped: a budget cap stopped the run')
  })

  it('degraded calls and timeout fallbacks show on the card with what they mean, and a run with only those is not clean', () => {
    render(<RunHealth state={state([call(1, 1, 'A', { degraded: true }), acted(2, 1, 'A', { fallback: true, fallback_reason: 'timeout' }), acted(3, 1, 'B')])} publicView={false} />)
    const card = screen.getByRole('region', { name: 'Run health' })
    expect(card).toHaveTextContent('Degraded calls')
    expect(card).toHaveTextContent('1 (the retry after a slow attempt sent lower thinking')
    expect(card).toHaveTextContent('Timeout fallbacks1: the system held or passed after two slow attempts. This fails the manipulation check')
    expect(card).not.toHaveTextContent('no fallback prices')
  })

  it('a run with only a degraded call, or only a timed-out PASS in the talk, is not clean', () => {
    const { unmount } = render(<RunHealth state={state([call(1, 1, 'A', { degraded: true })])} publicView={false} />)
    expect(screen.getByRole('region', { name: 'Run health' })).toHaveTextContent('Degraded calls')
    unmount()
    const talk = { seq: 2, t: 1, run_id: 'r', wall_time: 'w', tier: 'B', type: 'talk_replied', agent_id: 'A', outcome: 'pass', reason: 'timeout' } as unknown as SimEvent
    render(<RunHealth state={state([call(1, 1, 'A'), talk])} publicView={false} />)
    expect(screen.getByRole('region', { name: 'Run health' })).toHaveTextContent('Timeout fallbacks')
  })

  it('a run without degraded calls or timeout fallbacks does not show those rows', () => {
    render(<RunHealth state={state([call(1, 1, 'A', { error: 'timeout' })])} publicView={false} />)
    const card = screen.getByRole('region', { name: 'Run health' })
    expect(card).not.toHaveTextContent('Degraded calls')
    expect(card).not.toHaveTextContent('Timeout fallbacks')
  })

  it('fallback prices with no failed call are not "clean": the card lists them with the missing cause', () => {
    render(<RunHealth state={state([call(1, 1, 'A'), acted(2, 1, 'A', { fallback: true }), acted(3, 1, 'B')])} publicView={false} />)
    const card = screen.getByRole('region', { name: 'Run health' })
    expect(card).not.toHaveTextContent('no fallback prices')
    expect(within(card).getByRole('list', { name: 'Fallback prices by reason' })).toHaveTextContent('no failed call is recorded for that turn')
  })

  it('the public view shows no run health, because it has no model calls', () => {
    render(<RunHealth state={state([call(1, 1, 'A', { error: 'timeout' })])} publicView />)
    expect(screen.queryByRole('region', { name: 'Run health' })).toBeNull()
  })

  it('the metrics carry a caution when one is given, and none otherwise', () => {
    const stream = { state: foldEvents([]), phase: 'live', detail: null } as never
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = (caution: string | null) => (
      <QueryClientProvider client={qc}>
        <MetricsBar runId="r" stream={stream} status="completed" cursorT={0} experimentId={null} evaluationId={null} publicView={false} split="pilot" caution={caution} />
      </QueryClientProvider>
    )
    const { rerender } = render(view('Most prices are fallbacks.'))
    expect(screen.getByLabelText('Metrics caution')).toHaveTextContent('Caution: Most prices are fallbacks.')
    rerender(view(null))
    expect(screen.queryByLabelText('Metrics caution')).toBeNull()
  })
})

describe('the card of a run that ended on a settled round (UX review 2, item 3)', () => {
  it('says nothing about an incomplete round when the round shown is the last settled one', () => {
    const s = foldEvents(makeEpisode({ runId: 'r', kind: 'exploratory', seed: 1, rounds: 6, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null }))
    render(<AgentCard state={s} agentId="A" cursorT={6} publicView={false} final={{ t: 6, status: 'completed' }} />)
    expect(screen.getByRole('region', { name: 'Agent A' }).lastElementChild!.textContent).toMatch(/^R6 price \d/)
  })
})

describe('a run that ended in the middle of a round (UX review 2, item 3)', () => {
  const id = 'run_ENDMID'
  const all = makeEpisode({ runId: id, kind: 'exploratory', seed: 3, rounds: 12, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null })
  const cut = all.findIndex((e) => e.type === 'acted' && e.agent_id === 'A' && e.t === 6) + 1

  beforeAll(() => {
    const last = all[cut - 1]!
    const ended = { seq: cut, run_id: id, t: last.t, wall_time: last.wall_time, tier: 'B', type: 'run_ended', status: 'stopped', end_reason: 'budget_exceeded', episode_hash: 'sha256:x', cost_usd: 0.01, error: null } as unknown as SimEvent
    if (!mockRuns.some((x) => x.id === id)) mockRuns.push({ ...mockRuns.find((x) => x.id === 'run_MOCK02')!, id, header: foldEvents(all).header!, events: [...all.slice(0, cut), ended], live: false, startedAt: Date.now() - 1e6 })
  })

  it('says where it ended and what was settled, marks the unfinished round, and never says in progress', async () => {
    const router = createMemoryRouter([{ path: '/workbench/:runId', element: <WorkbenchPage /> }], { initialEntries: [`/workbench/${id}`] })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByText(/ended during R6; last settled round R5/)).toBeInTheDocument())
    expect(screen.queryByText(/in progress/)).toBeNull()
    expect(screen.getByRole('region', { name: 'Agent A' }).lastElementChild!.textContent).toMatch(/^R6 \(incomplete\) price \d/)
    expect(screen.getByRole('region', { name: 'Run health' })).toHaveTextContent('End: stopped: a budget cap stopped the run')
  })
})
